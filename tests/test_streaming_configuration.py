"""Streaming configuration is admitted before expensive backend acquisition."""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import TIMEOUT_MAX
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.qwen_backend import QwenTTSVoiceRouterBackend
from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_backend import MossTTSVoiceRouterBackend, PocketTTSVoiceRouterBackend
from vntts.voices import CharacterVoiceRegistry


class FloatValue:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __float__(self):
        self.calls += 1
        return self.value


class IntValue:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __int__(self):
        self.calls += 1
        return self.value


class StreamingConfigurationTest(unittest.TestCase):
    def test_invalid_durations_never_load_models_or_construct_caches(self):
        for backend, fields in (
            (
                PocketTTSVoiceRouterBackend,
                ("cached_stream_chunk_seconds", "stream_prefill_seconds"),
            ),
            (
                MossTTSVoiceRouterBackend,
                (
                    "cached_stream_chunk_seconds",
                    "streaming_interval",
                    "playback_consumer_join_timeout",
                ),
            ),
            (
                QwenTTSVoiceRouterBackend,
                (
                    "cached_stream_chunk_seconds",
                    "streaming_interval",
                    "playback_consumer_join_timeout",
                ),
            ),
        ):
            for name in fields:
                for value in (
                    10**400,
                    -(10**400),
                    float("nan"),
                    float("inf"),
                    -float("inf"),
                    "invalid",
                    None,
                    "nan",
                    "inf",
                    object(),
                ):
                    with self.subTest(
                        backend=backend.name,
                        option=name,
                        value_type=type(value).__name__,
                    ):
                        factory = Mock(side_effect=AssertionError("model reached"))
                        with (
                            patch("vntts.qwen_backend.sys.platform", "win32"),
                            patch(
                                "vntts.speech_backend.activate_pocket_tts_runtime"
                            ) as pocket,
                            patch(
                                "vntts.speech_backend.activate_moss_tts_runtime"
                            ) as moss,
                            patch("vntts.speech_backend.PersistentAudioCache") as cache,
                            self.assertRaisesRegex(
                                TTSConfigurationError, name + " must be a finite number"
                            ),
                        ):
                            backend(
                                CharacterVoiceRegistry(),
                                model_factory=factory,
                                **{name: value},
                            )
                        factory.assert_not_called()
                        pocket.assert_not_called()
                        moss.assert_not_called()
                        cache.assert_not_called()

    def test_invalid_first_frames_never_load_models(self):
        for backend in (MossTTSVoiceRouterBackend, QwenTTSVoiceRouterBackend):
            for value in (None, "invalid", float("nan"), float("inf"), -float("inf")):
                with self.subTest(backend=backend.name, value=value):
                    factory = Mock(side_effect=AssertionError("model reached"))
                    with (
                        patch("vntts.qwen_backend.sys.platform", "win32"),
                        self.assertRaisesRegex(
                            TTSConfigurationError,
                            "streaming_first_chunk_frames must be convertible to an integer",
                        ),
                    ):
                        backend(
                            CharacterVoiceRegistry(),
                            model_factory=factory,
                            streaming_first_chunk_frames=value,
                        )
                    factory.assert_not_called()

    def test_unrepresentable_join_timeout_never_loads_models(self):
        for backend in (MossTTSVoiceRouterBackend, QwenTTSVoiceRouterBackend):
            for value in (sys.float_info.max, TIMEOUT_MAX + 1):
                with self.subTest(backend=backend.name, value=value):
                    factory = Mock(side_effect=AssertionError("model reached"))
                    with (
                        patch("vntts.qwen_backend.sys.platform", "win32"),
                        self.assertRaisesRegex(
                            TTSConfigurationError,
                            "playback_consumer_join_timeout exceeds the thread timeout range",
                        ),
                    ):
                        backend(
                            CharacterVoiceRegistry(),
                            model_factory=factory,
                            playback_consumer_join_timeout=value,
                        )
                    factory.assert_not_called()

    def test_qwen_admits_streaming_configuration_before_download_or_device_setup(self):
        for name in (
            "cached_stream_chunk_seconds",
            "streaming_first_chunk_frames",
            "streaming_interval",
            "playback_consumer_join_timeout",
        ):
            with self.subTest(option=name):
                download = Mock(side_effect=AssertionError("download reached"))
                with (
                    patch("vntts.qwen_backend.sys.platform", "win32"),
                    patch("vntts.qwen_backend._require_cuda_device") as cuda,
                    patch.dict(
                        "sys.modules",
                        {
                            "huggingface_hub": SimpleNamespace(
                                snapshot_download=download
                            )
                        },
                    ),
                    self.assertRaisesRegex(TTSConfigurationError, name),
                ):
                    QwenTTSVoiceRouterBackend(
                        CharacterVoiceRegistry(), **{name: float("nan")}
                    )
                download.assert_not_called()
                cuda.assert_not_called()

    def test_qwen_platform_refusal_stays_first(self):
        with (
            patch("vntts.qwen_backend.sys.platform", "linux"),
            self.assertRaisesRegex(
                TTSConfigurationError, "requires Apple Silicon or Windows CUDA"
            ),
        ):
            QwenTTSVoiceRouterBackend(
                CharacterVoiceRegistry(), cached_stream_chunk_seconds=None
            )

    def test_valid_coercions_and_negative_floors_keep_model_rate(self):
        for backend in (
            PocketTTSVoiceRouterBackend,
            MossTTSVoiceRouterBackend,
            QwenTTSVoiceRouterBackend,
        ):
            for raw, seconds in (
                (0, 0),
                (-1, -1),
                (True, 1),
                (False, 0),
                ("0.125", 0.125),
                (b"0.125", 0.125),
                (bytearray(b"0.125"), 0.125),
                (0.125, 0.125),
            ):
                with (
                    self.subTest(backend=backend.name, value=raw),
                    TemporaryDirectory() as directory,
                    patch("vntts.qwen_backend.sys.platform", "win32"),
                    patch.dict("sys.modules", {"torch": SimpleNamespace()}),
                ):
                    model = SimpleNamespace(sample_rate=10)
                    factory = Mock(return_value=model)
                    options = {"cached_stream_chunk_seconds": raw}
                    if backend is PocketTTSVoiceRouterBackend:
                        options["stream_prefill_seconds"] = raw
                    else:
                        options.update(
                            streaming_interval=raw, playback_consumer_join_timeout=raw
                        )
                    result = backend(
                        CharacterVoiceRegistry(),
                        model_factory=factory,
                        persistent_audio_cache_directory=Path(directory) / "cache",
                        **options,
                    )
                    self.assertIs(result.model, model)
                    self.assertEqual(
                        result.cached_stream_chunk_samples, max(1, round(10 * seconds))
                    )
                    if backend is PocketTTSVoiceRouterBackend:
                        self.assertEqual(
                            result.stream_prefill_samples, max(1, round(10 * seconds))
                        )
                    else:
                        self.assertEqual(result.streaming_interval, max(0.08, seconds))
                        self.assertEqual(
                            result.playback_consumer_join_timeout, max(0.01, seconds)
                        )
                    self.assertEqual(factory.call_count, 1)

    def test_custom_coercions_run_once_per_option(self):
        for backend in (
            PocketTTSVoiceRouterBackend,
            MossTTSVoiceRouterBackend,
            QwenTTSVoiceRouterBackend,
        ):
            cached, other, join = FloatValue(0.125), FloatValue(0.25), FloatValue(0.5)
            frames = IntValue(3)
            options = {"cached_stream_chunk_seconds": cached}
            if backend is PocketTTSVoiceRouterBackend:
                options["stream_prefill_seconds"] = other
            else:
                options.update(
                    streaming_interval=other,
                    playback_consumer_join_timeout=join,
                    streaming_first_chunk_frames=frames,
                )
            with (
                self.subTest(backend=backend.name),
                TemporaryDirectory() as directory,
                patch("vntts.qwen_backend.sys.platform", "win32"),
                patch.dict("sys.modules", {"torch": SimpleNamespace()}),
            ):
                result = backend(
                    CharacterVoiceRegistry(),
                    model_factory=Mock(return_value=SimpleNamespace(sample_rate=24000)),
                    persistent_audio_cache_directory=Path(directory) / "cache",
                    **options,
                )
                self.assertEqual(result.cached_stream_chunk_samples, 3000)
                self.assertEqual(cached.calls, 1)
                self.assertEqual(other.calls, 1)
                if backend is not PocketTTSVoiceRouterBackend:
                    self.assertEqual(result.streaming_first_chunk_frames, 3)
                    self.assertEqual(frames.calls, 1)
                    self.assertEqual(join.calls, 1)

    def test_first_frame_coercion_and_floors_stay_compatible(self):
        for backend in (MossTTSVoiceRouterBackend, QwenTTSVoiceRouterBackend):
            for raw, expected in ((3.9, 3), ("3", 3), (True, 1), (False, 1), (-3, 1)):
                with (
                    self.subTest(backend=backend.name, value=raw),
                    TemporaryDirectory() as directory,
                    patch("vntts.qwen_backend.sys.platform", "win32"),
                    patch.dict("sys.modules", {"torch": SimpleNamespace()}),
                ):
                    result = backend(
                        CharacterVoiceRegistry(),
                        model_factory=Mock(
                            return_value=SimpleNamespace(sample_rate=48000)
                        ),
                        persistent_audio_cache_directory=Path(directory) / "cache",
                        streaming_first_chunk_frames=raw,
                    )
                    self.assertEqual(result.streaming_first_chunk_frames, expected)

    def test_derived_sample_overflow_is_refused_before_cache_construction(self):
        for backend, names in (
            (
                PocketTTSVoiceRouterBackend,
                ("cached_stream_chunk_seconds", "stream_prefill_seconds"),
            ),
            (MossTTSVoiceRouterBackend, ("cached_stream_chunk_seconds",)),
            (QwenTTSVoiceRouterBackend, ("cached_stream_chunk_seconds",)),
        ):
            for name in names:
                for value in (sys.float_info.max, -sys.float_info.max):
                    with self.subTest(
                        backend=backend.name, option=name, negative=value < 0
                    ):
                        factory = Mock(return_value=SimpleNamespace(sample_rate=48000))
                        with (
                            patch("vntts.qwen_backend.sys.platform", "win32"),
                            patch(
                                "vntts.speech_backend.PersistentAudioCache",
                                side_effect=AssertionError("cache reached"),
                            ) as cache,
                            self.assertRaisesRegex(
                                TTSConfigurationError,
                                name + " exceeds the sample count range",
                            ) as caught,
                        ):
                            backend(
                                CharacterVoiceRegistry(),
                                model_factory=factory,
                                **{name: value},
                            )
                        self.assertIsInstance(caught.exception.__cause__, OverflowError)
                        self.assertEqual(factory.call_count, 1)
                        cache.assert_not_called()

    def test_thread_limit_does_not_restrict_other_streaming_durations(self):
        for backend in (MossTTSVoiceRouterBackend, QwenTTSVoiceRouterBackend):
            with (
                self.subTest(backend=backend.name),
                TemporaryDirectory() as directory,
                patch("vntts.qwen_backend.sys.platform", "win32"),
                patch.dict("sys.modules", {"torch": SimpleNamespace()}),
            ):
                result = backend(
                    CharacterVoiceRegistry(),
                    model_factory=Mock(return_value=SimpleNamespace(sample_rate=48000)),
                    persistent_audio_cache_directory=Path(directory) / "cache",
                    playback_consumer_join_timeout=TIMEOUT_MAX,
                    streaming_interval=sys.float_info.max,
                )
                self.assertEqual(result.playback_consumer_join_timeout, TIMEOUT_MAX)
                self.assertEqual(result.streaming_interval, sys.float_info.max)
                self.assertEqual(result.streaming_first_chunk_frames, 4)
                self.assertEqual(result.cached_stream_chunk_samples, 9600)

    def test_representable_large_sample_count_uses_actual_low_rate(self):
        for backend in (
            PocketTTSVoiceRouterBackend,
            MossTTSVoiceRouterBackend,
            QwenTTSVoiceRouterBackend,
        ):
            with (
                self.subTest(backend=backend.name),
                TemporaryDirectory() as directory,
                patch("vntts.qwen_backend.sys.platform", "win32"),
                patch.dict("sys.modules", {"torch": SimpleNamespace()}),
            ):
                options = {"cached_stream_chunk_seconds": sys.float_info.max}
                if backend is PocketTTSVoiceRouterBackend:
                    options["stream_prefill_seconds"] = sys.float_info.max
                result = backend(
                    CharacterVoiceRegistry(),
                    model_factory=Mock(return_value=SimpleNamespace(sample_rate=1)),
                    persistent_audio_cache_directory=Path(directory) / "cache",
                    **options,
                )
                self.assertEqual(
                    result.cached_stream_chunk_samples, round(sys.float_info.max)
                )
                if backend is PocketTTSVoiceRouterBackend:
                    self.assertEqual(
                        result.stream_prefill_samples, round(sys.float_info.max)
                    )
