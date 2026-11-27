"""Foreground model transfers remain visible independently of runtime installs."""

import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tests.speech_worker_fixtures import FakeProcess
from vntts.huggingface_assets import AssetProgress
from vntts.model_activity import (
    begin_model_activity,
    get_model_activity,
    model_activities,
)
from vntts.qwen_backend import QwenTTSVoiceRouterBackend
from vntts.speech_backend import TTSConfigurationError, TTSSynthesisError
from vntts.speech_worker import (
    IsolatedSpeechBackend,
    _asset_progress_document,
    _asset_progress_value,
    _read_frame,
    _serialize_registry,
    _write_frame,
    worker_main,
)
from vntts.voices import CharacterVoiceRegistry


class ModelActivityTests(unittest.TestCase):
    def test_superseded_owner_cannot_report_or_clear_new_activity(self):
        first = begin_model_activity("fixture")
        event = AssetProgress("downloading", "weights", 12, 40)
        second = begin_model_activity("fixture", event)
        try:
            first.report(AssetProgress("loading", "old weights"))
            first.close()
            self.assertEqual(get_model_activity("fixture"), event)
            snapshot = model_activities()
            snapshot.clear()
            self.assertEqual(get_model_activity("fixture"), event)
        finally:
            second.close()
        second.report(event)
        self.assertIsNone(get_model_activity("fixture"))

    def test_backend_activities_are_independent(self):
        first = begin_model_activity("fixture-first")
        second = begin_model_activity("fixture-second")
        try:
            first.close()
            self.assertIsNone(get_model_activity("fixture-first"))
            self.assertEqual(
                get_model_activity("fixture-second"),
                AssetProgress("resolving", "fixture-second"),
            )
        finally:
            first.close()
            second.close()

    def test_worker_frame_preserves_unknown_totals_and_rejects_invalid_counts(self):
        event = AssetProgress("downloading", "weights", completed_files=2)
        document = _asset_progress_document(event)
        self.assertEqual(_asset_progress_value(document), event)
        for invalid in (-1, True, 1.5, "12"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(TTSConfigurationError):
                    _asset_progress_value({**document, "total_bytes": invalid})

    def test_qwen_local_loading_has_no_transfer_percentage(self):
        events = []
        model = SimpleNamespace(
            sample_rate=24_000,
            speech_tokenizer=SimpleNamespace(has_encoder=True),
            generate=lambda **_options: iter(()),
        )
        with (
            TemporaryDirectory() as directory,
            patch("vntts.qwen_backend.sys.platform", "darwin"),
        ):
            QwenTTSVoiceRouterBackend(
                CharacterVoiceRegistry(),
                model_factory=lambda _name, **_options: model,
                model_progress=events.append,
                persistent_audio_cache_directory=Path(directory) / "audio",
            )
        self.assertEqual(events, [AssetProgress("loading", "Qwen3-TTS model")])

    def test_worker_emits_typed_transfer_before_health(self):
        registry = CharacterVoiceRegistry()
        incoming, outgoing = BytesIO(), BytesIO()
        _write_frame(
            incoming,
            {
                "type": "initialize",
                "backend": "qwen-tts",
                "runtime_site": "/isolated/site-packages",
                "registry": _serialize_registry(registry),
                "options": {},
                "model_progress": True,
            },
        )
        incoming.seek(0)
        transfer = AssetProgress("downloading", "Qwen3-TTS model", 12, 80)

        def create_backend(registry, *, model_progress):
            model_progress(transfer)
            return SimpleNamespace(
                registry=registry,
                narrator_reference=None,
                sample_rate=24_000,
                render=lambda _request: None,
                prime=lambda _voice: True,
                set_live_mode_active=lambda active: active,
            )

        self.assertEqual(
            worker_main(
                input_stream=incoming,
                output_stream=outgoing,
                backend_classes={"qwen-tts": create_backend},
                required_modules={"qwen-tts": ()},
            ),
            0,
        )
        outgoing.seek(0)
        self.assertEqual(_read_frame(outgoing)[0], _asset_progress_document(transfer))
        self.assertEqual(_read_frame(outgoing)[0]["type"], "health")

    def test_parent_observes_transfer_then_clears_on_all_terminal_paths(self):
        transfer = AssetProgress("downloading", "Qwen3-TTS model", 12, 80)
        loading = AssetProgress("loading", "Qwen3-TTS model")
        for ending in ("ready", "failure", "cancelled", "legacy"):
            with self.subTest(ending=ending), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                process = FakeProcess(None)
                if ending != "legacy":
                    _write_frame(process.stdout, _asset_progress_document(transfer))
                    _write_frame(process.stdout, _asset_progress_document(loading))
                else:
                    _write_frame(
                        process.stdout,
                        {"type": "asset_progress", "message": "Legacy model loading"},
                    )
                _write_frame(
                    process.stdout,
                    {"type": "fatal", "error": "broken model"}
                    if ending == "failure"
                    else {
                        "type": "health",
                        "interpreter": str(root / "python"),
                        "prefix": str(root),
                        "sample_rate": 24_000,
                    },
                )
                process.stdout.seek(0)
                statuses, observed = [], []
                backend = IsolatedSpeechBackend(
                    "qwen-tts",
                    CharacterVoiceRegistry(),
                    defer_startup=True,
                    startup_progress=statuses.append,
                    process_factory=lambda *_args, **_options: process,
                )
                backend.deferred_startup = False
                backend.runtime_root = root
                backend.interpreter = root / "python"
                backend.runtime_site = root / "site-packages"
                original_next = backend._next_frame

                def read(*args, **options):
                    observed.append(get_model_activity("qwen-tts"))
                    return original_next(*args, **options)

                def cancelled():
                    return ending == "cancelled" and len(observed) >= 2

                try:
                    with (
                        patch(
                            "vntts.runtime_ownership.claim_runtime", return_value=None
                        ),
                        patch.object(backend, "_next_frame", side_effect=read),
                    ):
                        if ending in {"failure", "cancelled"}:
                            with self.assertRaises(
                                (TTSConfigurationError, TTSSynthesisError)
                            ):
                                backend._start_worker(cancellation=cancelled)
                            self.assertIsNotNone(process.poll())
                        else:
                            backend._start_worker()
                    self.assertIsNone(get_model_activity("qwen-tts"))
                    self.assertEqual(observed[0].phase, "resolving")
                    if ending != "legacy":
                        self.assertIn(transfer, observed)
                    if ending in {"ready", "failure"}:
                        self.assertIn(loading, observed)
                    if ending == "legacy":
                        self.assertIn("Legacy model loading", statuses)
                    process.stdin.seek(0)
                    self.assertTrue(_read_frame(process.stdin)[0]["model_progress"])
                finally:
                    backend.shutdown()


if __name__ == "__main__":
    unittest.main()
