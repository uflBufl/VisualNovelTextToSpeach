"""Check an installed backend environment without loading or downloading models."""

import argparse
import json
import os
import platform
import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from vntts.runtime_paths import RUNTIME_ENVIRONMENT_VARIABLES

ROOT = Path(__file__).resolve().parents[1]
CUDA_BACKENDS = {"moss-tts-delay", "moss-soundeffect-v2"}


def _forbid_model_loading(*_args, **_kwargs):
    raise RuntimeError("A dependency smoke test must never load model weights")


def check_runtime(backend, *, allow_unavailable_metal=False):
    runtime = ROOT / "backends" / backend / ".venv"
    if Path(sys.prefix).resolve() != runtime.resolve():
        raise RuntimeError(f"Run this check with the isolated {backend} environment")
    report = {"runtime": str(runtime), "python": platform.python_version()}
    if backend == "moss-soundeffect-v2":
        # The pipeline needs CUDA. Its CPU-host gate checks installed metadata and
        # real Torch, not GPU-only pipeline imports or model quality.
        report["package_version"] = version("moss-soundeffect-v2")
    else:
        from vntts.services.tts_engine import TTSConfigurationError
        from vntts.speech_worker import (
            probe_speech_runtime,
            resolve_speech_runtime_paths,
        )

        paths = resolve_speech_runtime_paths(backend, runtime)
        try:
            report["worker_dependencies"] = probe_speech_runtime(backend, paths)
        except TTSConfigurationError as error:
            if not (
                backend == "moss-tts"
                and allow_unavailable_metal
                and "[metal::load_device] No Metal device available." in str(error)
            ):
                raise
            report["unavailable_metal"] = str(error)

    if backend in CUDA_BACKENDS:
        import torch

        if not torch.version.cuda:
            raise RuntimeError("Expected the locked CUDA Torch build, not CPU Torch")
        if torch.cuda.is_available():
            raise RuntimeError("This negative smoke test requires no visible CUDA GPU")
        report["torch"] = torch.__version__
        report["cuda_runtime"] = torch.version.cuda
        if backend == "moss-tts-delay":
            from vntts.moss_delay_backend import MossTTSDelayVoiceRouterBackend
            from vntts.services.tts_engine import TTSConfigurationError
            from vntts.voices import CharacterVoiceRegistry

            loader = SimpleNamespace(from_pretrained=_forbid_model_loading)
            try:
                MossTTSDelayVoiceRouterBackend(
                    CharacterVoiceRegistry(),
                    require_cuda=True,
                    torch_module=torch,
                    auto_model=loader,
                    auto_processor=loader,
                )
            except TTSConfigurationError as error:
                if (
                    str(error)
                    != "MOSS Delay comparison requires CUDA; refusing CPU model loading"
                ):
                    raise
                report["no_cuda"] = str(error)
        else:
            from vntts.authoring.sound_effect_benchmark import benchmark_sound_effects
            from vntts.cuda_probe import CudaProbeError

            with TemporaryDirectory(prefix="vntts-runtime-smoke-") as directory:
                try:
                    benchmark_sound_effects(
                        ROOT / "samples/moss-soundeffect-v2-corpus.json",
                        Path(directory) / "output",
                        torch_module=torch,
                        pipeline_factory=_forbid_model_loading,
                    )
                except CudaProbeError as error:
                    if "no CUDA device is available" not in str(error):
                        raise
                    report["no_cuda"] = str(error)
        if "no_cuda" not in report:
            raise RuntimeError("CUDA backend failed to refuse model startup")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "backend", choices=[*RUNTIME_ENVIRONMENT_VARIABLES, "moss-soundeffect-v2"]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-unavailable-metal",
        action="store_true",
        help="report a hosted Mac without Metal as hardware-unavailable, not a passed import check",
    )
    arguments = parser.parse_args(argv)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    report = {
        "schema": "vntts.runtime-smoke-v1",
        "backend": arguments.backend,
        "model_rendered": False,
    }
    try:
        report.update(
            check_runtime(
                arguments.backend,
                allow_unavailable_metal=arguments.allow_unavailable_metal,
            )
        )
        report["status"] = (
            "hardware-unavailable" if "unavailable_metal" in report else "passed"
        )
    except Exception as error:
        report.update(
            status="failed", error_type=type(error).__name__, error=str(error)
        )
    payload = json.dumps(report, indent=2, sort_keys=True)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if report["status"] == "hardware-unavailable" and os.environ.get("GITHUB_ACTIONS"):
        print(
            "::warning title=MOSS import gate incomplete::Metal is unavailable. Validate MOSS imports and rendering on a real Metal host."
        )
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
