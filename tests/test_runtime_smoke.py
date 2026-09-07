import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from scripts.smoke_speech_runtime import ROOT, check_runtime, main
from tests.test_cuda_probe import FakeTorch


class RuntimeSmokeTest(unittest.TestCase):
    def test_uses_production_worker_probe_and_rejects_wrong_environment(self):
        runtime = ROOT / "backends/pocket-tts/.venv"
        with (
            patch("sys.prefix", str(runtime)),
            patch(
                "vntts.speech_worker.resolve_speech_runtime_paths",
                return_value=(runtime,),
            ),
            patch(
                "vntts.speech_worker.probe_speech_runtime", return_value={"modules": {}}
            ) as probe,
        ):
            self.assertEqual(
                check_runtime("pocket-tts")["worker_dependencies"], {"modules": {}}
            )
            probe.assert_called_once_with("pocket-tts", (runtime,))
        with patch("sys.prefix", str(ROOT)):
            with self.assertRaisesRegex(RuntimeError, "isolated"):
                check_runtime("pocket-tts")

    def test_cuda_candidates_exercise_production_refusal_without_model_loading(self):
        for backend in ("moss-tts-delay", "moss-soundeffect-v2"):
            with (
                self.subTest(backend=backend),
                patch("sys.prefix", str(ROOT / "backends" / backend / ".venv")),
                patch("scripts.smoke_speech_runtime.version", return_value="0.1.0"),
                patch(
                    "vntts.speech_worker.resolve_speech_runtime_paths", return_value=()
                ),
                patch("vntts.speech_worker.probe_speech_runtime", return_value={}),
                patch.dict("sys.modules", {"torch": FakeTorch(available=False)}),
                patch("scripts.smoke_speech_runtime._forbid_model_loading") as loading,
            ):
                self.assertIn("no_cuda", check_runtime(backend))
                loading.assert_not_called()
                for torch in (
                    FakeTorch(),
                    FakeTorch(available=False, cuda_runtime=None),
                ):
                    with patch.dict("sys.modules", {"torch": torch}):
                        with self.assertRaises(RuntimeError):
                            check_runtime(backend)
                with (
                    patch("vntts.moss_delay_backend.MossTTSDelayVoiceRouterBackend"),
                    patch(
                        "vntts.authoring.sound_effect_benchmark.benchmark_sound_effects"
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "failed to refuse"):
                        check_runtime(backend)

    def test_cli_records_failures_and_forces_offline_mode(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ),
            patch("builtins.print"),
        ):
            output = Path(directory) / "report.json"
            for failure in (None, ImportError("missing dependency")):
                with patch(
                    "scripts.smoke_speech_runtime.check_runtime",
                    return_value={},
                    side_effect=failure,
                ):
                    code = main(["pocket-tts", "--output", str(output)])
                report = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(code, 1 if failure else 0)
                self.assertEqual(report["status"], "failed" if failure else "passed")
                self.assertFalse(report["model_rendered"])
                self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
                self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
                if failure:
                    self.assertEqual(report["error_type"], "ImportError")

    def test_metal_absence_requires_explicit_opt_in_and_does_not_hide_other_errors(
        self,
    ):
        from vntts.services.tts_engine import TTSConfigurationError

        with (
            patch("sys.prefix", str(ROOT / "backends/moss-tts/.venv")),
            patch("vntts.speech_worker.resolve_speech_runtime_paths", return_value=()),
            patch("vntts.speech_worker.probe_speech_runtime") as probe,
        ):
            probe.side_effect = TTSConfigurationError(
                "[metal::load_device] No Metal device available."
            )
            with self.assertRaises(TTSConfigurationError):
                check_runtime("moss-tts")
            self.assertIn(
                "unavailable_metal",
                check_runtime("moss-tts", allow_unavailable_metal=True),
            )
            probe.side_effect = TTSConfigurationError("bad transformer import")
            with self.assertRaises(TTSConfigurationError):
                check_runtime("moss-tts", allow_unavailable_metal=True)
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ),
            patch("builtins.print"),
            patch(
                "scripts.smoke_speech_runtime.check_runtime",
                return_value={"unavailable_metal": "no GPU"},
            ),
        ):
            output = Path(directory) / "report.json"
            self.assertEqual(
                main(
                    ["moss-tts", "--allow-unavailable-metal", "--output", str(output)]
                ),
                0,
            )
            self.assertEqual(
                json.loads(output.read_text())["status"], "hardware-unavailable"
            )

    def test_ci_covers_all_runtime_projects_and_keeps_reports(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )
        job = workflow["jobs"]["speech-runtime-smoke"]
        rows = job["strategy"]["matrix"]["include"]
        self.assertEqual(
            {row["backend"] for row in rows},
            {path.parent.name for path in (ROOT / "backends").glob("*/pyproject.toml")},
        )
        self.assertEqual(
            {row["os"] for row in rows if row["backend"] == "pocket-tts"},
            {"macos-15", "windows-latest", "ubuntu-latest"},
        )
        self.assertFalse(job["strategy"]["fail-fast"])
        self.assertGreater(job["timeout-minutes"], 0)
        install = next(
            step for step in job["steps"] if "uv sync" in step.get("run", "")
        )
        self.assertIn("--locked", install["run"])
        smoke = next(
            step
            for step in job["steps"]
            if "scripts.smoke_speech_runtime" in step.get("run", "")
        )
        self.assertGreater(smoke["timeout-minutes"], 0)
        self.assertEqual(smoke["env"]["HF_HUB_OFFLINE"], "1")
        upload = job["steps"][-1]
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["path"], "runtime-smoke.json")


if __name__ == "__main__":
    unittest.main()
