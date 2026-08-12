import argparse
import json
import platform
from difflib import SequenceMatcher
from importlib import metadata
from pathlib import Path
from statistics import median
from time import perf_counter, process_time

from PIL import Image

from vntts.ocr_backend import RapidOCRBackend, TesseractOCRBackend
from vntts.settings import get_local_data_directory
from vntts.voices import CharacterVoiceRegistry, find_default_voice_manifest

default_output = get_local_data_directory() / "benchmarks" / "tesseract-ocr.json"


def _normalize(value):
    return " ".join((value or "").casefold().split())


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = round((len(ordered) - 1) * fraction)
    return ordered[position]


def load_expectations(path):
    if path is None:
        return {}
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("OCR benchmark expectations must be a JSON object")
    return document


def distribution_size_mb(names):
    paths = set()
    for name in names:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for relative_path in distribution.files or ():
            path = Path(distribution.locate_file(relative_path))
            if path.is_file():
                paths.add(path.resolve())
    return sum(path.stat().st_size for path in paths) / (1024 * 1024)


def benchmark_ocr(
    image_paths,
    *,
    backend=None,
    registry=None,
    repeats=3,
    warmups=1,
    minimum_confidence=0,
    language="eng",
    expectations=None,
    clock=perf_counter,
    cpu_clock=process_time,
):
    if repeats < 1 or warmups < 0:
        raise ValueError(
            "OCR benchmark repeats must be positive and warmups non-negative"
        )
    backend = backend or TesseractOCRBackend()
    expectations = expectations or {}
    samples = []
    all_latencies = []
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
        latencies = []
        cpu_times = []
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
        expected = expectations.get(image_path.name, {})
        expected_text = expected.get("text") if isinstance(expected, dict) else None
        expected_speaker = (
            expected.get("speaker") if isinstance(expected, dict) else None
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
            getattr(backend, "distribution_names", ())
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
                )
                if wall_ms > 0
            ),
        },
        "samples": samples,
    }


def write_report(report, output=default_output):
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def build_parser():
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


def main(argv=None):
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
    print(
        f"{report['backend']}: {summary['images']} image(s), median "
        f"{summary['median_latency_ms']:.1f} ms, p95 "
        f"{summary['p95_latency_ms']:.1f} ms"
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
