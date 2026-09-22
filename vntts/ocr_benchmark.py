import argparse
import json
import platform
from collections.abc import Callable, Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from importlib import metadata
from pathlib import Path
from statistics import median
from time import perf_counter, process_time
from typing import TypeAlias, TypedDict

from PIL import Image
from vntts_artifacts.atomic_io import atomic_write_json

from vntts.cli import cli_messages
from vntts.ocr import VoiceRegistry
from vntts.ocr_backend import OCRBackend, RapidOCRBackend, TesseractOCRBackend
from vntts.settings import get_local_data_directory
from vntts.voices import CharacterVoiceRegistry, find_default_voice_manifest

default_output = get_local_data_directory() / "benchmarks" / "tesseract-ocr.json"
PathInput: TypeAlias = str | Path
Expectations: TypeAlias = dict[str, object]


class TimingReport(TypedDict):
    median: float
    p95: float | None
    runs: list[float]


class OCRSampleReport(TypedDict):
    image: str
    width: int
    height: int
    latency_ms: TimingReport
    cpu_ms: TimingReport
    speaker: str
    text: str
    confidence: float
    profile: str
    speaker_match: bool | None
    text_similarity: float | None


class OCRSummaryReport(TypedDict):
    images: int
    median_latency_ms: float | None
    p95_latency_ms: float | None
    median_cpu_utilization_percent: float


class OCRBenchmarkReport(TypedDict):
    version: int
    backend: str
    platform: str
    python: str
    language: str
    warmups: int
    repeats: int
    installed_python_package_size_mb: float
    summary: OCRSummaryReport
    samples: list[OCRSampleReport]


def _normalize(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = round((len(ordered) - 1) * fraction)
    return ordered[position]


def load_expectations(path: PathInput | None) -> Expectations:
    if path is None:
        return {}
    document: object = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not all(
        isinstance(name, str) for name in document
    ):
        raise ValueError("OCR benchmark expectations must be a JSON object")
    return {name: value for name, value in document.items() if isinstance(name, str)}


def distribution_size_mb(names: Iterable[str]) -> float:
    paths: set[Path] = set()
    for name in names:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for relative_path in distribution.files or ():
            path = Path(str(distribution.locate_file(relative_path)))
            if path.is_file():
                paths.add(path.resolve())
    return sum(path.stat().st_size for path in paths) / (1024 * 1024)


def _distribution_names(backend: OCRBackend) -> tuple[str, ...]:
    names = getattr(backend, "distribution_names", ())
    if not isinstance(names, (list, tuple)) or not all(
        isinstance(name, str) for name in names
    ):
        raise ValueError("OCR backend distribution names must be text")
    return tuple(names)


def _expected_dialog(value: object, image_name: str) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise ValueError(f"OCR expectation for {image_name} must be an object")
    text = value.get("text")
    speaker = value.get("speaker")
    if text is not None and not isinstance(text, str):
        raise ValueError(f"OCR expected text for {image_name} must be text")
    if speaker is not None and not isinstance(speaker, str):
        raise ValueError(f"OCR expected speaker for {image_name} must be text")
    return text, speaker


def benchmark_ocr(
    image_paths: Iterable[PathInput],
    *,
    backend: OCRBackend | None = None,
    registry: VoiceRegistry | None = None,
    repeats: int = 3,
    warmups: int = 1,
    minimum_confidence: float = 0,
    language: str = "eng",
    expectations: Mapping[str, object] | None = None,
    clock: Callable[[], float] = perf_counter,
    cpu_clock: Callable[[], float] = process_time,
) -> OCRBenchmarkReport:
    if repeats < 1 or warmups < 0:
        raise ValueError(
            "OCR benchmark repeats must be positive and warmups non-negative"
        )
    backend = backend or TesseractOCRBackend()
    expectations = expectations or {}
    samples: list[OCRSampleReport] = []
    all_latencies: list[float] = []
    for image_path in image_paths:
        image_path = Path(image_path).expanduser().resolve()
        with Image.open(image_path) as source:
            image = source.convert("RGB")
        for _index in range(warmups):
            backend.recognize(
                image,
                registry,
                minimum_confidence=minimum_confidence,
                language=language,
            )
        latencies: list[float] = []
        cpu_times: list[float] = []
        result = None
        for _index in range(repeats):
            started = clock()
            cpu_started = cpu_clock()
            result = backend.recognize(
                image,
                registry,
                minimum_confidence=minimum_confidence,
                language=language,
            )
            cpu_times.append((cpu_clock() - cpu_started) * 1000)
            latencies.append((clock() - started) * 1000)
        all_latencies.extend(latencies)
        if result is None:
            raise RuntimeError("OCR benchmark produced no measured result")
        expected_text, expected_speaker = _expected_dialog(
            expectations.get(image_path.name), image_path.name
        )
        samples.append(
            {
                "image": str(image_path),
                "width": image.width,
                "height": image.height,
                "latency_ms": {
                    "median": median(latencies),
                    "p95": _percentile(latencies, 0.95),
                    "runs": latencies,
                },
                "cpu_ms": {
                    "median": median(cpu_times),
                    "p95": _percentile(cpu_times, 0.95),
                    "runs": cpu_times,
                },
                "speaker": result.character,
                "text": result.text,
                "confidence": result.confidence,
                "profile": result.profile,
                "speaker_match": (
                    _normalize(result.character) == _normalize(expected_speaker)
                    if expected_speaker is not None
                    else None
                ),
                "text_similarity": (
                    SequenceMatcher(
                        None,
                        _normalize(result.text),
                        _normalize(expected_text),
                    ).ratio()
                    if expected_text is not None
                    else None
                ),
            }
        )
    return {
        "version": 1,
        "backend": backend.name,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "language": language,
        "warmups": warmups,
        "repeats": repeats,
        "installed_python_package_size_mb": distribution_size_mb(
            _distribution_names(backend)
        ),
        "summary": {
            "images": len(samples),
            "median_latency_ms": median(all_latencies) if all_latencies else None,
            "p95_latency_ms": _percentile(all_latencies, 0.95),
            "median_cpu_utilization_percent": median(
                cpu_ms / wall_ms * 100
                for sample in samples
                for cpu_ms, wall_ms in zip(
                    sample["cpu_ms"]["runs"],
                    sample["latency_ms"]["runs"],
                    strict=True,
                )
                if wall_ms > 0
            ),
        },
        "samples": samples,
    }


def write_report(
    report: Mapping[str, object], output: PathInput = default_output
) -> Path:
    output = Path(output).expanduser().resolve()
    atomic_write_json(output, report)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark an OCR backend")
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--expectations", type=Path)
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--language", default="eng")
    parser.add_argument(
        "--backend",
        choices=("tesseract", "rapidocr"),
        default="tesseract",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    manifest = find_default_voice_manifest()
    registry = CharacterVoiceRegistry.from_file(manifest) if manifest else None
    backend = (
        RapidOCRBackend() if arguments.backend == "rapidocr" else TesseractOCRBackend()
    )
    report = benchmark_ocr(
        arguments.images,
        backend=backend,
        registry=registry,
        repeats=arguments.repeats,
        warmups=arguments.warmups,
        language=arguments.language,
        expectations=load_expectations(arguments.expectations),
    )
    output = write_report(report, arguments.output)
    summary = report["summary"]
    return cli_messages(
        (
            f"{report['backend']}: {summary['images']} image(s), median "
            f"{summary['median_latency_ms']:.1f} ms, p95 "
            f"{summary['p95_latency_ms']:.1f} ms",
            output,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
