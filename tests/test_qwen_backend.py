"""Qwen's reference transcript must survive routing into its model call."""

import unittest
import wave
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from vntts.pregeneration_queue import PregenerationQueueError, _write_effective_voices
from vntts.pregeneration_voices import _materialize_voice_catalog
from vntts.qwen_backend import QwenTTSVoiceRouterBackend
from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_worker import (
    _registry_from_document,
    _serialize_registry,
    create_qwen_worker_backend,
)
from vntts.synthesis import SynthesisCompletion, SynthesisRequest
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class QwenBackendTest(unittest.TestCase):
    def test_qwen_reuses_the_installed_mlx_runtime(self):
        root = Path("/tmp/test-moss-mlx-runtime")
        registry = CharacterVoiceRegistry()
        with (
            patch(
                "vntts.runtime_installation.ensure_speech_runtime",
                return_value=(root, None, None),
            ) as prepare,
            patch(
                "vntts.speech_worker._isolated_backend_constructor",
                return_value="worker",
            ) as construct,
        ):
            self.assertEqual(create_qwen_worker_backend(registry), "worker")
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args, ("moss-tts",))
        self.assertEqual(construct.call_args.args, ("qwen-tts", registry))
        self.assertEqual(construct.call_args.kwargs["runtime_directory"], root)

    def test_reference_text_reaches_model_and_is_required(self):
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.wav"
            with wave.open(str(reference), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(b"\0\0" * 24000)

            registry = CharacterVoiceRegistry(
                [
                    CharacterVoice(
                        "Narrator",
                        "Narrator",
                        references=(reference,),
                        reference_transcript="The exact original line.",
                    )
                ]
            )
            restored = _registry_from_document(_serialize_registry(registry))
            calls = []

            class FakeModel:
                sample_rate = 24000
                speech_tokenizer = SimpleNamespace(has_encoder=True)

                def generate(self, **options):
                    calls.append(options)
                    yield SimpleNamespace(
                        audio=np.zeros(2400, dtype=np.float32), token_count=10
                    )

            with patch("vntts.qwen_backend.sys.platform", "darwin"):
                backend = QwenTTSVoiceRouterBackend(
                    restored,
                    narrator_reference=reference,
                    model_factory=lambda _name, lazy=False: FakeModel(),
                    persistent_audio_cache_directory=Path(directory) / "cache",
                )
                result = backend.render(
                    SynthesisRequest(voice="Narrator", text="A new line.")
                ).collect()
                self.assertEqual(result.completion, SynthesisCompletion.COMPLETE)
                self.assertEqual(calls[0]["ref_text"], "The exact original line.")
                self.assertEqual(calls[0]["ref_audio"], str(reference.resolve()))
                self.assertFalse(calls[0]["stream"])

                missing = CharacterVoiceRegistry(
                    [CharacterVoice("Narrator", "Narrator", references=(reference,))]
                )
                backend.registry = missing
                with self.assertRaisesRegex(TTSConfigurationError, "exact transcript"):
                    backend.render(
                        SynthesisRequest(voice="Narrator", text="Another line.")
                    )

    def test_offline_reference_carries_transcript_or_fails_early(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reference = root / "reference.wav"
            with wave.open(str(reference), "wb") as output:
                output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                output.writeframes(b"\0\0" * 24000)
            digest = sha256(reference.read_bytes()).hexdigest()
            voice = CharacterVoice(
                "Narrator",
                "Narrator",
                references=(reference,),
                reference_root=root,
                reference_transcript="The original line.",
            )
            staging = root / "staging"
            staging.mkdir()
            effective = {
                "routes": {"Narrator": (voice, (digest,), "Narrator")},
                "narrator_roles": (),
                "line_voice_characters": {},
            }
            entries = _write_effective_voices(staging, effective, backend="qwen-tts")
            self.assertEqual(
                entries[0]["vntts.reference_transcript"], "The original line."
            )
            effective["routes"]["Narrator"] = (
                CharacterVoice(
                    "Narrator", "Narrator", references=(reference,), reference_root=root
                ),
                (digest,),
                "Narrator",
            )
            missing_staging = root / "missing-staging"
            missing_staging.mkdir()
            with self.assertRaisesRegex(PregenerationQueueError, "exact transcript"):
                _write_effective_voices(missing_staging, effective, backend="qwen-tts")

    def test_voice_catalog_snapshot_keeps_reference_text(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reference = root / "reference.wav"
            with wave.open(str(reference), "wb") as output:
                output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                output.writeframes(b"\0\0" * 24000)
            registry = CharacterVoiceRegistry(
                [
                    CharacterVoice(
                        "Matilda",
                        "Matilda",
                        references=(reference,),
                        reference_root=root,
                        reference_transcript="The original line.",
                    )
                ]
            )
            store = SimpleNamespace(
                path_for=lambda _job_id: root / "job" / "state.json"
            )
            manifest = _materialize_voice_catalog(
                store, SimpleNamespace(job_id="job"), registry
            )
            restored = CharacterVoiceRegistry.from_file(manifest)
            self.assertEqual(
                restored.resolve("Matilda").reference_transcript,
                "The original line.",
            )
