"""Exercise native-build comparison orchestration without a native runtime."""

import hashlib
import io
import json
import os
import unittest
import zipfile
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from scripts import moss_native_compare as compare
from scripts import moss_native_pause_probe as probe
from tests.test_moss_native_pause_probe import _FakeBackend, clean_wav_bytes
from vntts.services.tts_engine import TTSConfigurationError
from vntts.synthesis import SynthesisCachePolicy, SynthesisCompletion


class _FailedBackend(_FakeBackend):
    instances = []

    def render(self, request):
        self.requests.append(request)
        self._http("POST", "/tts", {"text": request.text})
        if len(self.requests) == 2:
            raise RuntimeError("deliberate failure")
        return self._result(request, SynthesisCompletion.COMPLETE)


class _CancelledBackend(_FakeBackend):
    instances = []

    def render(self, request):
        self.requests.append(request)
        self._http("POST", "/tts", {"text": request.text})
        if len(self.requests) == 2:
            raise KeyboardInterrupt
        return self._result(request, SynthesisCompletion.COMPLETE)


class _LimitedBackend(_FakeBackend):
    instances = []

    def render(self, request):
        self.requests.append(request)
        self._http("POST", "/tts", {"text": request.text})
        return self._result(request, SynthesisCompletion.LIMITED)


class MossNativeCompareTest(unittest.TestCase):
    def _download_pair(
        self,
        root,
        *,
        mismatch=False,
        checksum_error=False,
        experiment="local-gpu",
        candidate_threads=8,
    ):
        for label, (variant, _) in zip(
            ("baseline", "candidate"), compare.EXPERIMENTS[experiment], strict=True
        ):
            name = f"moss-native-{variant}-windows-x64"
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as archive:
                archive.writestr("moss-tts-server.exe", label.encode())
                archive.writestr("dependency.dll", b"keep beside executable")
                archive.writestr(
                    "VNTTS-BUILD.json",
                    json.dumps(
                        {
                            "variant": variant,
                            "upstream": "pinned",
                            "llama": "pinned",
                            "vntts": label if mismatch else "same-run",
                            "patch_sha256": "timing-patch",
                            "local_gpu_patch_sha256": "local-patch",
                            "aux_threads_patch_sha256": "threads-patch",
                            "auxiliary_threads": candidate_threads
                            if variant.endswith("aux8")
                            else 4,
                            "persistent_aux_cpu_pool": "OFF",
                            "local_decoder_gpu": "ON"
                            if "local-gpu" in variant
                            else "OFF",
                        }
                    ),
                )
            checksum = (
                "0" * 64
                if checksum_error
                else hashlib.sha256(inner.getvalue()).hexdigest()
            )
            with zipfile.ZipFile(root / f"{name}.zip", "w") as archive:
                archive.writestr(f"{name}.zip", inner.getvalue())
                archive.writestr(f"{name}.zip.sha256", checksum)

    def test_short_launch_extracts_both_layers_and_only_checks_versions(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._download_pair(root)
            options = compare._parser().parse_args(["--downloads", temporary])
            with patch.object(
                compare.subprocess,
                "run",
                side_effect=[
                    SimpleNamespace(stdout="openmoss 0.3.0-vntts-timing1\n"),
                    SimpleNamespace(stdout="openmoss 0.3.0-vntts-timing1-localgpu\n"),
                ],
            ) as execute:
                compare.prepare_downloads(options)
            self.assertEqual(execute.call_count, 2)
            for call in execute.call_args_list:
                self.assertEqual(call.args[0][1:], ["--version"])
                self.assertEqual(call.kwargs["timeout"], 15)
            self.assertEqual(options.baseline.read_bytes(), b"baseline")
            self.assertEqual(options.candidate.read_bytes(), b"candidate")
            self.assertTrue((options.candidate.parent / "dependency.dll").is_file())
            self.assertTrue(options.output.parent.is_dir())
            self.assertFalse(options.output.exists())
            self.assertIsNone(options.reference)
            self.assertIsNone(options.model)

    def test_codec_threads_uses_approved_gpu_baseline_and_eight_worker_candidate(self):
        with TemporaryDirectory() as temporary:
            self._download_pair(Path(temporary), experiment="codec-threads")
            options = compare._parser().parse_args(
                ["--downloads", temporary, "--experiment", "codec-threads"]
            )
            with patch.object(
                compare.subprocess,
                "run",
                side_effect=[
                    SimpleNamespace(stdout="openmoss 0.3.0-vntts-timing1-localgpu\n"),
                    SimpleNamespace(
                        stdout="openmoss 0.3.0-vntts-timing1-localgpu-aux8\n"
                    ),
                ],
            ) as execute:
                compare.prepare_downloads(options)
            self.assertEqual(execute.call_count, 2)
            self.assertIn("timing-local-gpu", options.baseline.parts)
            self.assertIn("timing-local-gpu-aux8", options.candidate.parts)

    def test_shutdown_summary_does_not_promote_missing_evidence(self):
        runs = [
            {
                "variant": variant,
                "report": {"server_shutdown": {"confirmed_exited": True}},
            }
            for variant in compare.ORDER
        ]
        self.assertTrue(compare.summarize(runs)["owned_servers_confirmed_stopped"])
        runs[0]["report"]["server_shutdown"]["confirmed_exited"] = False
        self.assertFalse(compare.summarize(runs)["owned_servers_confirmed_stopped"])
        runs[0]["report"].clear()
        self.assertIsNone(compare.summarize(runs)["owned_servers_confirmed_stopped"])

    def test_codec_threads_rejects_wrong_worker_count_before_startup(self):
        with TemporaryDirectory() as temporary:
            self._download_pair(
                Path(temporary), experiment="codec-threads", candidate_threads=4
            )
            options = compare._parser().parse_args(
                ["--downloads", temporary, "--experiment", "codec-threads"]
            )
            with patch.object(compare.subprocess, "run") as execute:
                with self.assertRaisesRegex(ValueError, "codec-thread settings"):
                    compare.prepare_downloads(options)
                execute.assert_not_called()

    def test_short_launch_rejects_bad_downloads_before_executing_anything(self):
        for failure in ("missing", "checksum", "different-run", "unsafe-zip"):
            with self.subTest(failure=failure), TemporaryDirectory() as temporary:
                root = Path(temporary)
                if failure != "missing":
                    self._download_pair(
                        root,
                        mismatch=failure == "different-run",
                        checksum_error=failure == "checksum",
                    )
                if failure == "unsafe-zip":
                    with zipfile.ZipFile(
                        root / "moss-native-timing-windows-x64.zip", "a"
                    ) as archive:
                        archive.writestr("../escape.exe", b"bad")
                options = compare._parser().parse_args(["--downloads", temporary])
                with patch.object(compare.subprocess, "run") as execute:
                    with self.assertRaises((ValueError, TTSConfigurationError)):
                        compare.prepare_downloads(options)
                    execute.assert_not_called()
                self.assertFalse((root / "escape.exe").exists())

    def test_short_launch_rejects_partial_pair_and_cpu_only_before_setup(self):
        for arguments in (["--baseline", "one.exe"], ["--gpu-layers", "0"]):
            with (
                self.subTest(arguments=arguments),
                patch.object(compare, "run") as render,
            ):
                self.assertEqual(compare.main(arguments), 1)
                render.assert_not_called()

    def test_short_launch_reports_interrupt_and_wrong_version_without_rendering(self):
        for response, code in (
            (KeyboardInterrupt(), 130),
            (SimpleNamespace(stdout="wrong"), 1),
        ):
            with self.subTest(code=code), TemporaryDirectory() as temporary:
                self._download_pair(Path(temporary))
                with (
                    patch.object(compare.subprocess, "run", side_effect=[response]),
                    patch.object(compare, "run") as render,
                ):
                    self.assertEqual(compare.main(["--downloads", temporary]), code)
                    render.assert_not_called()

    def test_summary_retains_first_reference_encoding_without_inventing_cache_hits(
        self,
    ):
        runs = [
            {
                "variant": variant,
                "report": {
                    "attempts": [
                        {
                            "text": text,
                            "completion": "complete",
                            "result": {"cache_source": "fresh-generation"},
                            "raw_response": {"http_status": 200},
                            "native": {"reference_encoding_s": 13.0}
                            if index == 0
                            else {},
                        }
                        for index, (_, text) in enumerate(probe.TEXTS)
                    ]
                },
            }
            for variant in compare.ORDER
        ]
        cases = compare.summarize(runs)["cases"]
        for variant in ("baseline", "candidate"):
            self.assertEqual(
                cases[0][variant]["phases_seconds_median"]["reference_encoding_s"],
                13.0,
            )
            self.assertIsNone(
                cases[1][variant]["phases_seconds_median"]["reference_encoding_s"]
            )
        runs[0]["report"]["attempts"][0]["native"].clear()
        self.assertIsNone(
            compare.summarize(runs)["cases"][0]["baseline"]["phases_seconds_median"][
                "reference_encoding_s"
            ]
        )

    def _options(self, root):
        reference = root / "reference.wav"
        reference.write_bytes(clean_wav_bytes())
        model = root / "model.gguf"
        model.write_bytes(b"GGUF test model")
        sidecar = root / "model.extras.gguf"
        sidecar.write_bytes(b"GGUF test sidecar")
        baseline = root / "baseline.exe"
        baseline.write_bytes(b"baseline")
        candidate = root / "candidate.exe"
        candidate.write_bytes(b"candidate")
        return SimpleNamespace(
            baseline=baseline,
            candidate=candidate,
            model=model,
            reference=reference,
            output=root / "comparison",
            gpu_layers=-1,
        )

    @staticmethod
    def _path_check(_model):
        executable = Path(os.environ["VNTTS_MOSS_CPP_EXECUTABLE"])
        model = Path(os.environ["VNTTS_MOSS_GGUF"])
        return executable, model, model.with_suffix(".extras.gguf")

    def _run(self, options, backend):
        runner = partial(
            probe.run,
            backend_factory=backend,
            path_check=self._path_check,
            settings_loader=lambda: SimpleNamespace(tts_model=None),
        )
        return compare.run(options, probe_runner=runner, path_check=self._path_check)

    def test_abba_uses_four_fresh_cacheless_stable_runs_and_archives_wavs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = self._options(root)
            _FakeBackend.instances.clear()
            environment = {
                "VNTTS_MOSS_CPP_EXECUTABLE": "original-server",
                "VNTTS_MOSS_GGUF": "original-model",
                "VNTTS_MOSS_GPU_LAYERS": "original-layers",
                "VNTTS_MOSS_AUX_CPU": "original-aux",
                "VNTTS_MOSS_CONTEXT": "original-context",
            }
            with patch.dict(os.environ, environment):
                self.assertEqual(self._run(options, _FakeBackend), 0)
                self.assertEqual(
                    {name: os.environ[name] for name in environment}, environment
                )

            report = json.loads((options.output / "report.json").read_text())
            self.assertTrue(report["complete"])
            self.assertTrue(report["all_requests_complete"])
            self.assertEqual(report["order"], list(compare.ORDER))
            self.assertEqual(
                [run["variant"] for run in report["runs"]], list(compare.ORDER)
            )
            self.assertEqual(len(_FakeBackend.instances), 4)
            for backend in _FakeBackend.instances:
                self.assertTrue(backend.shutdown_called)
                self.assertEqual(len(backend.requests), len(probe.TEXTS))
                self.assertTrue(all(request.seed == 1 for request in backend.requests))
                self.assertTrue(
                    all(
                        request.cache_policy is SynthesisCachePolicy.BYPASS
                        for request in backend.requests
                    )
                )
                self.assertTrue(
                    all(
                        request.generation_profile == "stable"
                        for request in backend.requests
                    )
                )
            self.assertEqual(report["controls"]["sampling"]["audio_temperature"], 1.7)
            self.assertTrue(
                all(
                    attempt["sampling"]["audio_temperature"] == 1.7
                    for run in report["runs"]
                    for attempt in run["report"]["attempts"]
                )
            )
            self.assertTrue(
                all(
                    case["baseline"]["phases_seconds_median"]["gen_s"] is None
                    and case["candidate"]["phases_seconds_median"]["gen_s"] is None
                    for case in report["summary"]["cases"]
                )
            )
            with zipfile.ZipFile(options.output.with_suffix(".zip")) as archive:
                names = set(archive.namelist())
            self.assertNotIn("reference-input.wav", names)
            self.assertNotIn("model.gguf", names)
            self.assertNotIn("model.extras.gguf", names)
            self.assertNotIn("baseline.exe", names)
            self.assertNotIn("candidate.exe", names)
            for run in report["runs"]:
                for attempt in run["report"]["attempts"]:
                    self.assertIn(
                        f"{run['directory']}/{attempt['files']['raw_wav']['path']}",
                        names,
                    )

    def test_mismatched_build_manifests_stop_before_generation(self):
        for key in ("vntts", "local_gpu_patch_sha256"):
            with self.subTest(key=key), TemporaryDirectory() as temporary:
                root = Path(temporary)
                options = self._options(root)
                for variant in ("baseline", "candidate"):
                    directory = root / variant
                    directory.mkdir()
                    executable = directory / "server.exe"
                    getattr(options, variant).rename(executable)
                    setattr(options, variant, executable)
                    (directory / "VNTTS-BUILD.json").write_text(
                        json.dumps(
                            {
                                "upstream": "same",
                                "llama": "same",
                                "vntts": "same",
                                "patch_sha256": "same",
                                key: variant,
                            }
                        ),
                        encoding="utf-8-sig",
                    )
                _FakeBackend.instances.clear()
                self.assertEqual(self._run(options, _FakeBackend), 1)
                self.assertEqual(_FakeBackend.instances, [])
                report = json.loads((options.output / "report.json").read_text())
                self.assertIn(f"Build manifests differ at {key}", report["error"])
                self.assertTrue(options.output.with_suffix(".zip").is_file())

    def test_failed_and_cancelled_probe_runs_preserve_partial_audio_and_reports(self):
        cases = ((_FailedBackend, 1, "failed"), (_CancelledBackend, 130, "cancelled"))
        for backend, exit_code, completion in cases:
            with self.subTest(completion=completion), TemporaryDirectory() as temporary:
                root = Path(temporary)
                options = self._options(root)
                backend.instances.clear()

                self.assertEqual(self._run(options, backend), exit_code)

                report = json.loads((options.output / "report.json").read_text())
                self.assertFalse(report["complete"])
                self.assertFalse(report["all_requests_complete"])
                self.assertEqual(len(report["runs"]), 1)
                attempts = report["runs"][0]["report"]["attempts"]
                self.assertEqual(
                    [attempt["completion"] for attempt in attempts],
                    ["complete", completion],
                )
                self.assertIsNone(report["summary"]["cases"][0]["raw_wav_identity"])
                self.assertIsNone(
                    report["summary"]["cases"][0]["candidate_over_baseline"]
                )
                with zipfile.ZipFile(options.output.with_suffix(".zip")) as archive:
                    names = set(archive.namelist())
                for attempt in attempts:
                    self.assertIn(
                        f"{report['runs'][0]['directory']}/{attempt['files']['raw_wav']['path']}",
                        names,
                    )

    def test_limited_runs_finish_orchestration_but_fail_qualification_without_ratio(
        self,
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = self._options(root)
            _LimitedBackend.instances.clear()

            self.assertEqual(self._run(options, _LimitedBackend), 1)

            report = json.loads((options.output / "report.json").read_text())
            self.assertTrue(report["complete"])
            self.assertFalse(report["all_requests_complete"])
            self.assertEqual(len(report["runs"]), 4)
            self.assertTrue(
                all(
                    attempt["completion"] == "limited"
                    for run in report["runs"]
                    for attempt in run["report"]["attempts"]
                )
            )
            self.assertTrue(
                all(
                    case["candidate_over_baseline"] is None
                    for case in report["summary"]["cases"]
                )
            )


if __name__ == "__main__":
    unittest.main()
