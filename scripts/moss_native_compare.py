"""Compare two existing native MOSS builds without changing app settings."""

import argparse
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from statistics import median
from types import SimpleNamespace

from scripts import moss_native_pause_probe as probe
from vntts.moss_cpp_backend import MossCppVoiceRouterBackend, moss_cpp_paths
from vntts.moss_cpp_installation import _extract_runtime
from vntts.runtime_config import initialize_voice_registry
from vntts.settings import load_app_settings

ORDER = ("baseline", "candidate", "candidate", "baseline")


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", type=Path, help="Existing baseline server EXE (advanced)"
    )
    parser.add_argument(
        "--candidate", type=Path, help="Existing candidate server EXE (advanced)"
    )
    parser.add_argument(
        "--downloads",
        type=Path,
        default=Path.home() / "Downloads",
        help="Folder containing the two downloaded CI ZIPs; defaults to ~/Downloads",
    )
    parser.add_argument(
        "--model", type=Path, help="Existing GGUF; defaults to saved configuration"
    )
    parser.add_argument(
        "--reference", type=Path, help="Defaults to the saved narrator reference"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="New output directory; default: unique comparison folder in Downloads",
    )
    parser.add_argument(
        "--gpu-layers",
        type=int,
        choices=(-1, 0),
        default=-1,
        help=(
            "-1: automatic GPU backbone; 0: CPU only. Requests CPU auxiliary; "
            "the opt-in Local GPU build selectively offloads the frame model."
        ),
    )
    return parser


def _check_builds(builds):
    if all(builds.values()):
        keys = ["upstream", "llama", "vntts", "patch_sha256"]
        if any("local_gpu_patch_sha256" in build for build in builds.values()):
            keys.append("local_gpu_patch_sha256")
        for key in keys:
            if not builds["baseline"].get(key) or builds["baseline"].get(key) != builds[
                "candidate"
            ].get(key):
                raise ValueError(
                    f"Build manifests differ at {key}; use artifacts from the same workflow run"
                )


def prepare_downloads(options):
    """Set up the existing comparison without starting a model server."""
    if bool(options.baseline) != bool(options.candidate):
        raise ValueError("Provide both --baseline and --candidate, or neither")
    if not options.baseline and options.gpu_layers == 0:
        raise ValueError("The Local GPU comparison requires --gpu-layers -1")
    downloads = options.downloads.expanduser().resolve()
    if options.baseline and options.output:
        return
    variants = {"baseline": "timing", "candidate": "timing-local-gpu"}
    if not options.baseline:
        for variant in variants.values():
            archive = downloads / f"moss-native-{variant}-windows-x64.zip"
            if not archive.is_file():
                raise ValueError(
                    f"Download missing: {archive}. Download both same-run artifacts "
                    "listed in scripts/native/README.md, or use --downloads FOLDER."
                )
    downloads.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="moss-native-", dir=downloads))
    print(f"Comparison work folder: {work}", flush=True)
    if not options.baseline:
        builds = {}
        for label, variant in variants.items():
            print(
                f"Preparing {label}: checking and extracting the downloaded ZIP...",
                flush=True,
            )
            name = f"moss-native-{variant}-windows-x64"
            unpacked = work / variant / "download"
            _extract_runtime(downloads / f"{name}.zip", unpacked, None)
            inner = unpacked / f"{name}.zip"
            expected = (
                (unpacked / f"{name}.zip.sha256").read_text(encoding="ascii").strip()
            )
            if probe._sha256(inner).lower() != expected.lower():
                raise ValueError(
                    f"Checksum mismatch: {inner}; download that artifact again"
                )
            runtime = work / variant / "runtime"
            _extract_runtime(inner, runtime, None)
            build = json.loads(
                (runtime / "VNTTS-BUILD.json").read_text(encoding="utf-8-sig")
            )
            if build.get("variant") != variant or not build.get(
                "local_gpu_patch_sha256"
            ):
                raise ValueError(
                    f"Wrong or obsolete {label} build; download the current same-run pair"
                )
            builds[label] = build
            setattr(options, label, runtime / "moss-tts-server.exe")
        _check_builds(builds)
        for label, suffix in (("baseline", ""), ("candidate", "-localgpu")):
            print(f"Checking {label} executable (no model loaded)...", flush=True)
            result = subprocess.run(
                [str(getattr(options, label)), "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            expected = f"openmoss 0.3.0-vntts-timing1{suffix}"
            if result.stdout.strip() != expected:
                raise ValueError(
                    f"Wrong {label} version: {result.stdout.strip()!r}; expected {expected}"
                )
    if options.output is None:
        options.output = work / "comparison"


def _identity(path):
    path = path.resolve()
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": probe._sha256(path),
    }


def _unchanged(identity):
    stat = Path(identity["path"]).stat()
    if (stat.st_size, stat.st_mtime_ns) != (identity["bytes"], identity["mtime_ns"]):
        raise ValueError(f"Input changed during comparison: {identity['path']}")


def _median(values):
    finite = [
        x
        for x in values
        if isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
        and x >= 0
    ]
    return median(finite) if finite and len(finite) == len(values) else None


def summarize(runs):
    """Summarize matching cases only; failed/limited output is never a speed win."""
    groups = {}
    for variant in ("baseline", "candidate"):
        selected = [run for run in runs if run["variant"] == variant]
        groups[variant] = {
            "startup_seconds_median": _median(
                [run["report"].get("startup_seconds") for run in selected]
            )
        }
    comparisons = []
    for index, (label, text) in enumerate(probe.TEXTS):
        rows = {}
        for variant in groups:
            attempts = [
                attempt
                for run in runs
                if run["variant"] == variant
                for attempt in run["report"].get("attempts", [])
                if attempt["text"] == text
            ]
            valid = len(attempts) == 2 and all(
                item["completion"] == "complete"
                and item.get("result", {}).get("cache_source") == "fresh-generation"
                and item.get("raw_response", {}).get("http_status") == 200
                and not item.get("raw_quality_error")
                for item in attempts
            )
            rows[variant] = {
                "complete_fresh_attempts": valid,
                "elapsed_seconds_median": _median(
                    [item.get("elapsed_seconds") for item in attempts]
                )
                if valid
                else None,
                "raw_wav_hashes": [
                    item.get("raw_response", {}).get("sha256") for item in attempts
                ],
                "phases_seconds_median": {
                    key: _median(
                        [(item.get("native") or {}).get(key) for item in attempts]
                    )
                    if valid
                    else None
                    for key in (
                        "reference_encoding_s",
                        "prefill_s",
                        "gen_s",
                        "gen_backbone_s",
                        "gen_frame_decoder_s",
                        "gen_input_embedding_s",
                        "decode_s",
                    )
                },
                "observed_native_rss_peak_bytes": max(
                    (
                        ((item.get("native") or {}).get("resources") or {})
                        .get("native_process", {})
                        .get("rss_bytes_peak")
                        or 0
                        for item in attempts
                    ),
                    default=0,
                )
                or None,
                "native_avg_cores_used_median": _median(
                    [
                        ((item.get("native") or {}).get("resources") or {})
                        .get("native_process", {})
                        .get("avg_cores_used")
                        for item in attempts
                    ]
                )
                if valid
                else None,
            }
        baseline, candidate = rows["baseline"], rows["candidate"]
        a, b = baseline["elapsed_seconds_median"], candidate["elapsed_seconds_median"]
        hashes = baseline["raw_wav_hashes"] + candidate["raw_wav_hashes"]
        comparisons.append(
            {
                "case": label,
                "phase": "process-cold" if index == 0 else "warm",
                **rows,
                "candidate_over_baseline": b / a if a and b is not None else None,
                "raw_wav_identity": len(set(hashes)) == 1
                if len(hashes) == 4 and None not in hashes
                else None,
            }
        )
    compute = [run["report"].get("compute") for run in runs]
    return {
        "startup": groups,
        "cases": comparisons,
        "same_reported_compute": len(set(compute)) == 1
        if len(compute) == 4 and all(compute)
        else None,
        "qualification": "Measurements only; no automatic performance or voice approval.",
        "output_code_identity": "unavailable: native API returns WAV, not codec codes",
    }


def run(
    options,
    *,
    probe_runner=probe.run,
    path_check=moss_cpp_paths,
    settings_loader=load_app_settings,
    registry_initializer=initialize_voice_registry,
):
    output = options.output.expanduser().resolve()
    archive = output.parent / f"{output.name}.zip"
    if output.exists() or archive.exists():
        raise ValueError(
            "Output directory or archive already exists; choose a new --output"
        )
    output.mkdir(parents=True)
    report = {
        "schema": "vntts.native-build-comparison",
        "schema_version": 1,
        "order": list(ORDER),
        "runs": [],
        "complete": False,
        "controls": {
            "seed": 1,
            "gpu_layers": options.gpu_layers,
            "aux_cpu": 1,
            "context": 4096,
            "cache": "bypass",
            "sampling": dict(MossCppVoiceRouterBackend._generation_profiles["stable"]),
        },
        "limitations": [
            "Process-cold is not OS/disk-cache cold.",
            "Different warm cases are compared only against the same text.",
            "Timing/resource availability is explicit; missing is not zero.",
            "Request cancellation, idle CPU and real model load/unload need separate qualification.",
        ],
    }
    code = 0
    try:
        settings = (
            settings_loader()
            if options.reference is None or options.model is None
            else None
        )
        reference = (
            options.reference.expanduser().resolve()
            if options.reference
            else probe._saved_narrator_reference(settings, registry_initializer)[0]
        )
        report["reference"] = _identity(reference)
        snapshot = output / "reference-input.wav"
        shutil.copyfile(reference, snapshot)
        if probe._sha256(snapshot) != report["reference"]["sha256"]:
            raise ValueError("Reference changed while copying")
        preflight = probe.analyze_reference(snapshot)
        report["reference_preflight"] = preflight
        if preflight["objective_preflight"] != "pass":
            raise ValueError(
                "Reference preflight failed: "
                + ", ".join(preflight["rejection_reasons"])
            )
        model_name = options.model if options.model is not None else settings.tts_model
        paths = {}
        for variant in ("baseline", "candidate"):
            with probe._configured_native_paths(
                getattr(options, variant), options.model
            ):
                paths[variant] = path_check(model_name)
        if paths["baseline"][1:] != paths["candidate"][1:]:
            raise ValueError("Both builds must use the same model and sidecar")
        print(
            "Checking model and executable identities (large GGUF files may take a moment)...",
            flush=True,
        )
        baseline, model, sidecar = paths["baseline"]
        report["inputs"] = {
            "baseline": _identity(baseline),
            "candidate": _identity(paths["candidate"][0]),
            "model": _identity(model),
            "sidecar": _identity(sidecar),
        }
        builds = {}
        for variant in ("baseline", "candidate"):
            directory = paths[variant][0].parent
            manifest = directory / "VNTTS-BUILD.json"
            builds[variant] = (
                json.loads(manifest.read_text(encoding="utf-8-sig"))
                if manifest.is_file()
                else None
            )
            for file in sorted(directory.iterdir()):
                if file.is_file() and (
                    file.suffix.lower() == ".dll" or file.name == "VNTTS-BUILD.json"
                ):
                    report["inputs"][f"{variant}/{file.name}"] = _identity(file)
        report["build_manifests"] = builds
        _check_builds(builds)
        if (
            report["inputs"]["baseline"]["sha256"]
            == report["inputs"]["candidate"]["sha256"]
        ):
            raise ValueError("Baseline and candidate executables are identical")
        controls = {
            "VNTTS_MOSS_GPU_LAYERS": str(options.gpu_layers),
            "VNTTS_MOSS_AUX_CPU": "1",
            "VNTTS_MOSS_CONTEXT": "4096",
        }
        for index, variant in enumerate(ORDER, 1):
            for identity in report["inputs"].values():
                _unchanged(identity)
            folder = output / f"run-{index}-{variant}"
            print(
                f"[{index}/4] {variant}: fresh server, 1 process-cold + 2 warm phrases",
                flush=True,
            )
            probe_options = SimpleNamespace(
                reference=snapshot,
                model=model,
                executable=paths[variant][0],
                output=folder,
            )
            with probe._configured_native_paths(None, None, extra=controls):
                code = probe_runner(
                    probe_options,
                    sampling_profiles={"stable": report["controls"]["sampling"]},
                )
            child = json.loads((folder / "report.json").read_text(encoding="utf-8"))
            report["runs"].append(
                {
                    "variant": variant,
                    "directory": folder.name,
                    "exit_code": code,
                    "report": child,
                }
            )
            if child.get("reference_sha256") != report["reference"]["sha256"]:
                raise ValueError("Probe did not use the comparison reference snapshot")
            report["summary"] = summarize(report["runs"])
            probe._write_json(output / "report.json", report)
            if code:
                break
        for identity in report["inputs"].values():
            _unchanged(identity)
        report["complete"] = len(report["runs"]) == 4 and code == 0
    except KeyboardInterrupt:
        report["interrupted"] = True
        code = 130
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        print(report["error"], flush=True)
        code = 1
    finally:
        report["summary"] = summarize(report["runs"])
        report["all_requests_complete"] = report["complete"] and all(
            case[variant]["complete_fresh_attempts"]
            for case in report["summary"]["cases"]
            for variant in ("baseline", "candidate")
        )
        if code == 0 and not report["all_requests_complete"]:
            code = 1
        report["exit_code"] = code
        probe._write_json(output / "report.json", report)
        probe._write_json(output / "build.json", probe.collect_build_identity())
        probe._write_archive(output, archive, recursive=True)
    print(
        f"Comparison archive: {archive} (generated speech included; nothing uploaded)",
        flush=True,
    )
    return code


def main(argv=None):
    parser = _parser()
    options = parser.parse_args(argv)
    try:
        prepare_downloads(options)
        return run(options)
    except KeyboardInterrupt:
        print(
            "Comparison interrupted; any existing work folder is retained.", flush=True
        )
        return 130
    except Exception as error:
        print(f"Comparison failed: {type(error).__name__}: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
