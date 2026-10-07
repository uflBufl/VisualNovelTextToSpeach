"""Compare two existing native MOSS builds without changing app settings."""

import argparse
import json
import math
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import NotRequired, Protocol, TypedDict

from scripts import moss_native_pause_probe as probe
from vntts.moss_cpp_backend import MossCppVoiceRouterBackend, moss_cpp_paths
from vntts.moss_cpp_installation import _extract_runtime
from vntts.runtime_config import initialize_voice_registry
from vntts.settings import AppSettings, load_app_settings


class _ComparisonOptions(argparse.Namespace):
    baseline: Path | None
    candidate: Path | None
    downloads: Path
    experiment: str
    model: Path | None
    reference: Path | None
    output: Path | None
    gpu_layers: int


class _ProbeRunner(Protocol):
    def __call__(
        self,
        options: probe._ProbeOptions,
        *,
        sampling_profiles: probe._SamplingProfiles,
    ) -> int: ...


class _InputIdentity(TypedDict):
    path: str
    bytes: int
    mtime_ns: int
    sha256: str


class _Run(TypedDict):
    variant: str
    report: dict[str, object]
    directory: NotRequired[str]
    exit_code: NotRequired[int]


type _Metric = int | float


class _Startup(TypedDict):
    startup_seconds_median: _Metric | None


class _ComparisonRow(TypedDict):
    complete_fresh_attempts: bool
    elapsed_seconds_median: _Metric | None
    raw_wav_hashes: list[str | None]
    phases_seconds_median: dict[str, _Metric | None]
    observed_native_rss_peak_bytes: int | None
    native_avg_cores_used_median: _Metric | None


class _Case(TypedDict):
    case: str
    phase: str
    baseline: _ComparisonRow
    candidate: _ComparisonRow
    candidate_over_baseline: float | None
    raw_wav_identity: bool | None


class _Summary(TypedDict):
    startup: dict[str, _Startup]
    cases: list[_Case]
    same_reported_compute: bool | None
    owned_servers_confirmed_stopped: bool | None
    qualification: str
    output_code_identity: str


class _Controls(TypedDict):
    seed: int
    gpu_layers: int
    aux_cpu: int
    context: int
    cache: str
    sampling: dict[str, float]


class _ComparisonReport(TypedDict):
    schema: str
    schema_version: int
    order: list[str]
    runs: list[_Run]
    complete: bool
    controls: _Controls
    limitations: list[str]
    reference: NotRequired[_InputIdentity]
    reference_preflight: NotRequired[probe.ReferenceQualityReport]
    inputs: NotRequired[dict[str, _InputIdentity]]
    build_manifests: NotRequired[dict[str, dict[str, object] | None]]
    summary: NotRequired[_Summary]
    interrupted: NotRequired[bool]
    error: NotRequired[str]
    all_requests_complete: NotRequired[bool]
    exit_code: NotRequired[int]


ORDER = ("baseline", "candidate", "candidate", "baseline")
EXPERIMENTS = {
    "local-gpu": (("timing", ""), ("timing-local-gpu", "-localgpu")),
    "codec-threads": (
        ("timing-local-gpu", "-localgpu"),
        ("timing-local-gpu-aux8", "-localgpu-aux8"),
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        choices=EXPERIMENTS,
        default="local-gpu",
        help="Downloaded build pair: GPU frame model, or 4 vs 8 codec CPU workers",
    )
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


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Native comparison evidence must be a JSON object")
    return value


def _read_evidence(path: Path) -> dict[str, object]:
    payload: object = json.loads(path.read_text(encoding="utf-8-sig"))
    return _mapping(payload)


def _attempts(report: Mapping[str, object]) -> list[dict[str, object]]:
    values = report.get("attempts", [])
    if not isinstance(values, list):
        raise ValueError("Native comparison attempts must be a JSON list")
    attempts = [_mapping(value) for value in values]
    for attempt in attempts:
        for field in ("result", "raw_response"):
            if field in attempt:
                _mapping(attempt[field])
        _native_process(attempt)
    return attempts


def _native_process(attempt: Mapping[str, object]) -> dict[str, object]:
    native = _mapping(attempt.get("native") or {})
    resources = _mapping(native.get("resources") or {})
    return _mapping(resources.get("native_process", {}))


def _rss_peak(attempt: Mapping[str, object]) -> int | None:
    value = _native_process(attempt).get("rss_bytes_peak")
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _output_directory(path: Path | None) -> Path:
    if path is None:
        raise ValueError("Native comparison requires an output directory")
    return path.expanduser().resolve()


def _check_builds(builds: Mapping[str, dict[str, object] | None]) -> None:
    baseline, candidate = builds["baseline"], builds["candidate"]
    if baseline and candidate:
        keys = ["upstream", "llama", "vntts", "patch_sha256"]
        for key in ("local_gpu_patch_sha256", "aux_threads_patch_sha256"):
            if any(key in build for build in (baseline, candidate)):
                keys.append(key)
        for key in keys:
            if not baseline.get(key) or baseline.get(key) != candidate.get(key):
                raise ValueError(
                    f"Build manifests differ at {key}; use artifacts from the same workflow run"
                )


def prepare_downloads(options: _ComparisonOptions) -> None:
    """Set up the existing comparison without starting a model server."""
    if bool(options.baseline) != bool(options.candidate):
        raise ValueError("Provide both --baseline and --candidate, or neither")
    if not options.baseline and options.gpu_layers == 0:
        raise ValueError("The Local GPU comparison requires --gpu-layers -1")
    downloads = options.downloads.expanduser().resolve()
    if options.baseline and options.output:
        return
    variants = dict(
        zip(("baseline", "candidate"), EXPERIMENTS[options.experiment], strict=True)
    )
    if not options.baseline:
        for variant, _ in variants.values():
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
        builds: dict[str, dict[str, object] | None] = {}
        for label, (variant, _) in variants.items():
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
            build = _read_evidence(runtime / "VNTTS-BUILD.json")
            if build.get("variant") != variant or not build.get(
                "local_gpu_patch_sha256"
            ):
                raise ValueError(
                    f"Wrong or obsolete {label} build; download the current same-run pair"
                )
            if options.experiment == "codec-threads" and (
                not build.get("aux_threads_patch_sha256")
                or build.get("auxiliary_threads") != (4 if label == "baseline" else 8)
                or build.get("persistent_aux_cpu_pool") != "OFF"
                or build.get("local_decoder_gpu") != "ON"
            ):
                raise ValueError(
                    f"Wrong {label} codec-thread settings; download the 4/8-worker pair"
                )
            builds[label] = build
            setattr(options, label, runtime / "moss-tts-server.exe")
        _check_builds(builds)
        for label, (_, suffix) in variants.items():
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


def _identity(path: Path) -> _InputIdentity:
    path = path.resolve()
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": probe._sha256(path),
    }


def _unchanged(identity: _InputIdentity) -> None:
    stat = Path(identity["path"]).stat()
    if (stat.st_size, stat.st_mtime_ns) != (identity["bytes"], identity["mtime_ns"]):
        raise ValueError(f"Input changed during comparison: {identity['path']}")


def _median(values: Sequence[object]) -> _Metric | None:
    finite = [
        x
        for x in values
        if isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
        and x >= 0
    ]
    return median(finite) if finite and len(finite) == len(values) else None


def summarize(runs: Sequence[_Run]) -> _Summary:
    """Summarize matching cases only; failed/limited output is never a speed win."""
    groups: dict[str, _Startup] = {}
    for variant in ("baseline", "candidate"):
        selected = [run for run in runs if run["variant"] == variant]
        groups[variant] = {
            "startup_seconds_median": _median(
                [run["report"].get("startup_seconds") for run in selected]
            )
        }
    comparisons: list[_Case] = []
    for index, (label, text) in enumerate(probe.TEXTS):
        rows: dict[str, _ComparisonRow] = {}
        for variant in groups:
            attempts = [
                attempt
                for run in runs
                if run["variant"] == variant
                for attempt in _attempts(run["report"])
                if attempt["text"] == text
            ]
            valid = len(attempts) == 2 and all(
                item["completion"] == "complete"
                and _mapping(item.get("result", {})).get("cache_source")
                == "fresh-generation"
                and _mapping(item.get("raw_response", {})).get("http_status") == 200
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
                    value
                    if isinstance(
                        value := _mapping(item.get("raw_response", {})).get("sha256"),
                        str,
                    )
                    else None
                    for item in attempts
                ],
                "phases_seconds_median": {
                    key: _median(
                        [
                            _mapping(item.get("native") or {}).get(key)
                            for item in attempts
                        ]
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
                    (_rss_peak(item) or 0 for item in attempts),
                    default=0,
                )
                or None,
                "native_avg_cores_used_median": _median(
                    [_native_process(item).get("avg_cores_used") for item in attempts]
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
                "baseline": baseline,
                "candidate": candidate,
                "candidate_over_baseline": b / a if a and b is not None else None,
                "raw_wav_identity": len(set(hashes)) == 1
                if len(hashes) == 4 and None not in hashes
                else None,
            }
        )
    compute = [
        value if isinstance(value := run["report"].get("compute"), str) else None
        for run in runs
    ]
    stopped = [
        value
        if isinstance(
            value := _mapping(run["report"].get("server_shutdown", {})).get(
                "confirmed_exited"
            ),
            bool,
        )
        else None
        for run in runs
    ]
    return {
        "startup": groups,
        "cases": comparisons,
        "same_reported_compute": len(set(compute)) == 1
        if len(compute) == 4 and all(compute)
        else None,
        "owned_servers_confirmed_stopped": all(stopped)
        if len(stopped) == 4 and all(value is not None for value in stopped)
        else None,
        "qualification": "Measurements only; no automatic performance or voice approval.",
        "output_code_identity": "unavailable: native API returns WAV, not codec codes",
    }


def _admit_probe_run(
    runs: list[_Run],
    variant: str,
    directory: str,
    exit_code: int,
    report: dict[str, object],
) -> _Summary:
    captured: _Run = {
        "variant": variant,
        "directory": directory,
        "exit_code": exit_code,
        "report": report,
    }
    summary = summarize((*runs, captured))
    runs.append(captured)
    return summary


def run(
    options: _ComparisonOptions,
    *,
    probe_runner: _ProbeRunner = probe.run,
    path_check: Callable[[str | Path | None], tuple[Path, Path, Path]] = moss_cpp_paths,
    settings_loader: Callable[[], AppSettings] = load_app_settings,
    registry_initializer: probe._RegistryInitializer = initialize_voice_registry,
) -> int:
    output = _output_directory(options.output)
    archive = output.parent / f"{output.name}.zip"
    if output.exists() or archive.exists():
        raise ValueError(
            "Output directory or archive already exists; choose a new --output"
        )
    output.mkdir(parents=True)
    report: _ComparisonReport = {
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
        model_name = probe._model_name(options.model, settings)
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
        builds: dict[str, dict[str, object] | None] = {}
        for variant in ("baseline", "candidate"):
            directory = paths[variant][0].parent
            manifest = directory / "VNTTS-BUILD.json"
            builds[variant] = _read_evidence(manifest) if manifest.is_file() else None
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
            probe_options = probe._ProbeOptions(
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
            child = _read_evidence(folder / "report.json")
            if child.get("reference_sha256") != report["reference"]["sha256"]:
                raise ValueError("Probe did not use the comparison reference snapshot")
            report["summary"] = _admit_probe_run(
                report["runs"], variant, folder.name, code, child
            )
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    options = parser.parse_args(argv, namespace=_ComparisonOptions())
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
