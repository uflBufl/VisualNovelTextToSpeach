import io
import json
import unittest
import wave
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np

from scripts import moss_native_pause_probe as probe
from tests.test_pregeneration_audition import clean_wav_bytes
from vntts.synthesis import (
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)


def _stereo_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(
            (np.array([[1000, -1000]], dtype="<i2").repeat(480, 0)).tobytes()
        )
    return output.getvalue()


class _Stream:
    def __init__(self, result):
        self.result = result

    def collect(self):
        return self.result


class _FakeBackend:
    instances = []

    def __init__(self, _registry, **options):
        self.registry = _registry
        self.options = options
        self.requests = []
        self.shutdown_called = False
        type(self).instances.append(self)

    def _http(self, method, path, body=None, *, timeout=None):
        assert timeout is None
        if path == "/tts":
            return 200, {"X-MOSS-Audio-Frames": "2"}, _stereo_wav()
        raise AssertionError(path)

    def render(self, request):
        self.requests.append(request)
        self._http("POST", "/tts", {"text": request.text})
        return self._result(request, SynthesisCompletion.COMPLETE)

    @staticmethod
    def _result(request, completion):
        pcm = np.tile(np.array([[0.1, -0.1]], dtype=np.float32), (480, 1))
        return _Stream(
            SynthesisResult(
                pcm=pcm,
                sample_rate=48000,
                completion=completion,
                limits=SynthesisLimits(*probe.moss_generation_limits(request.text)),
                timing=SynthesisTiming(1.0, 2.0),
                diagnostics=SynthesisDiagnostics(
                    "moss-cpp",
                    "fresh-generation",
                    request.generation_profile,
                    request.seed,
                    1,
                    len(pcm),
                ),
            )
        )

    def shutdown(self):
        self.shutdown_called = True


class _LimitedThenInterruptedBackend(_FakeBackend):
    def render(self, request):
        self.requests.append(request)
        self._http("POST", "/tts", {"text": request.text})
        if len(self.requests) == 2:
            raise KeyboardInterrupt
        return self._result(request, SynthesisCompletion.LIMITED)


def _options(root):
    reference = root / "reference.wav"
    reference.write_bytes(clean_wav_bytes())
    return SimpleNamespace(
        reference=reference, output=root / "probe", model=None, executable=None
    )


class MossNativePauseProbeTest(unittest.TestCase):
    def test_probe_writes_cacheless_stereo_comparison_and_stops_backend(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            _FakeBackend.instances.clear()

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    path_check=lambda _model: (
                        Path("server"),
                        Path("model.gguf"),
                        Path("codec.gguf"),
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                0,
            )

            report = json.loads((options.output / "report.json").read_text())
            self.assertEqual(report["http_capture_method"], "_http")
            self.assertEqual(len(report["attempts"]), 6)
            joined = next(
                item
                for item in report["attempts"]
                if item["id"].endswith("stable-joined")
            )
            sentences = next(
                item
                for item in report["attempts"]
                if item["id"].endswith("stable-sentences")
            )
            self.assertEqual(
                joined["production_limits"], sentences["production_limits"]
            )
            self.assertEqual(joined["raw_quality"]["mono"]["analysis_version"], 2)
            for attempt in report["attempts"]:
                self.assertTrue(
                    (options.output / attempt["files"]["raw_wav"]["path"]).is_file()
                )
                self.assertTrue(
                    (
                        options.output / attempt["files"]["output_mono_wav"]["path"]
                    ).is_file()
                )
                self.assertTrue((options.output / f"{attempt['id']}.json").is_file())
            backend = _FakeBackend.instances[0]
            self.assertTrue(backend.shutdown_called)
            self.assertFalse((options.output / ".cache-disabled").exists())
            self.assertTrue(all(request.seed == 1 for request in backend.requests))
            self.assertTrue(
                all(
                    request.cache_policy is probe.SynthesisCachePolicy.BYPASS
                    for request in backend.requests
                )
            )
            archive = options.output.with_suffix(".zip")
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as bundle:
                self.assertIn("report.json", bundle.namelist())
                self.assertIn(joined["files"]["raw_wav"]["path"], bundle.namelist())
                self.assertIn(
                    joined["files"]["output_mono_wav"]["path"], bundle.namelist()
                )

    def test_probe_never_overwrites_an_existing_output_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            options.output.mkdir()
            marker = options.output / "keep.txt"
            marker.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "already exists"):
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_omitted_reference_uses_only_the_saved_narrator_reference(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "saved.wav"
            reference.write_bytes(clean_wav_bytes())
            options = SimpleNamespace(
                reference=None,
                output=root / "probe",
                model=Path("model.gguf"),
                executable=None,
            )
            registry = SimpleNamespace(
                resolve=lambda name: (
                    SimpleNamespace(references=(reference,))
                    if name == "Narrator"
                    else None
                )
            )
            _FakeBackend.instances.clear()

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    path_check=lambda _model: (
                        Path("server"),
                        Path("model.gguf"),
                        Path("codec.gguf"),
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model="saved-model"),
                    registry_initializer=lambda _settings: registry,
                ),
                0,
            )
            self.assertIs(_FakeBackend.instances[0].registry, registry)

    def test_omitted_reference_rejects_an_ambiguous_saved_narrator(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "one.wav", root / "two.wav"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            options = SimpleNamespace(
                reference=None,
                output=root / "probe",
                model=Path("model.gguf"),
                executable=None,
            )
            registry = SimpleNamespace(
                resolve=lambda _name: SimpleNamespace(references=(first, second))
            )

            with self.assertRaisesRegex(ValueError, "pass --reference PATH"):
                probe.run(
                    options,
                    settings_loader=lambda: SimpleNamespace(tts_model="saved-model"),
                    registry_initializer=lambda _settings: registry,
                )

    def test_limited_and_interrupted_attempts_keep_reports_raw_audio_and_archive(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            _LimitedThenInterruptedBackend.instances.clear()

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_LimitedThenInterruptedBackend,
                    path_check=lambda _model: (
                        Path("server"),
                        Path("model.gguf"),
                        Path("codec.gguf"),
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                130,
            )
            report = json.loads((options.output / "report.json").read_text())
            self.assertTrue(report["interrupted"])
            self.assertEqual(
                [item["completion"] for item in report["attempts"]],
                ["limited", "cancelled"],
            )
            self.assertEqual(
                report["attempts"][0]["result"]["limits"],
                report["attempts"][0]["production_limits"],
            )
            self.assertTrue(report["attempts"][1]["interrupted"])
            self.assertTrue(
                (
                    options.output / report["attempts"][1]["files"]["raw_wav"]["path"]
                ).is_file()
            )
            self.assertEqual(
                len(_LimitedThenInterruptedBackend.instances[0].requests), 2
            )
            self.assertTrue(_LimitedThenInterruptedBackend.instances[0].shutdown_called)
            self.assertTrue(options.output.with_suffix(".zip").is_file())

    def test_missing_native_assets_never_construct_the_backend(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            _FakeBackend.instances.clear()

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    path_check=lambda _model: (_ for _ in ()).throw(
                        RuntimeError("assets missing")
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                1,
            )
            report = json.loads((options.output / "report.json").read_text())
            self.assertIn("assets missing", report["error"])
            self.assertFalse(_FakeBackend.instances)
            self.assertTrue(options.output.with_suffix(".zip").is_file())

    def test_unusable_reference_is_reported_without_loading_native_runtime(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            options.reference.write_bytes(
                clean_wav_bytes(seconds=0.06075, sample_rate=24000)
            )
            _FakeBackend.instances.clear()

            def no_native_probe(_model):
                self.fail("invalid reference must fail before native runtime checks")

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    path_check=no_native_probe,
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                1,
            )
            self.assertFalse(_FakeBackend.instances)
            with zipfile.ZipFile(options.output.with_suffix(".zip")) as archive:
                report = json.loads(archive.read("report.json"))
            self.assertEqual(report["attempts"], [])
            self.assertEqual(report["reference_preflight"]["duration_seconds"], 0.061)
            self.assertIn("duration-under-1-second", report["error"])
            self.assertIn("--reference PATH", report["error"])


if __name__ == "__main__":
    unittest.main()
