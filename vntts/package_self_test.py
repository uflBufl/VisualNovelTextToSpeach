import importlib
import json
import os
import subprocess
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.atomic_io import atomic_write_json

from vntts.cli import CLIReportResult
from vntts.onboarding import probe_tesseract
from vntts.release_runtime import PROBE_MODULES, runtime_probe_script
from vntts.runtime_paths import (
    configure_bundled_dependencies,
    find_bundled_espeak,
    get_bundle_root,
)
from vntts.settings import get_local_data_directory
from vntts.speech_worker import IsolatedSpeechBackend, resolve_speech_runtime_paths
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisRequest,
)
from vntts.voices import CharacterVoiceRegistry

required_modules = (
    "PySide6",
    "PIL",
    "TTS.api",
    "TTS.tts.configs.xtts_config",
    "mss",
    "pynput",
    "pytesseract",
    "sounddevice",
    "torch",
    "torchaudio",
)

_PUBLIC_POCKET_ARTIFACTS = {
    (
        "kyutai/pocket-tts-without-voice-cloning",
        "d29db7978e464fb90cb3359ee0c69a273b9142cc",
        "languages/english/model.safetensors",
    ): "model",
    (
        "kyutai/pocket-tts-without-voice-cloning",
        "d29db7978e464fb90cb3359ee0c69a273b9142cc",
        "languages/english/tokenizer.model",
    ): "tokenizer",
    (
        "kyutai/pocket-tts-without-voice-cloning",
        "e81d79e8194ad4c7ce879c87a4258ef20cbf2487",
        "languages/english/embeddings/alba.safetensors",
    ): "voice",
}


def probe_espeak(executable):
    completed = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=15,
    )
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else "available"


def probe_bundled_pocket_runtime(bundle_root=None, runner=subprocess.run):
    bundle_root = get_bundle_root() if bundle_root is None else Path(bundle_root)
    if bundle_root is None:
        raise RuntimeError("Pocket runtime provenance requires a frozen bundle")
    allowed_root = (bundle_root / "speech-runtimes").resolve()
    runtime_root, interpreter, runtime_site = resolve_speech_runtime_paths("pocket-tts")
    if runtime_root != allowed_root / "pocket-tts":
        raise RuntimeError(
            f"Pocket runtime is outside the frozen bundle: {runtime_root}"
        )
    completed = runner(
        [str(interpreter), "-I", "-B", "-c", runtime_probe_script()],
        capture_output=True,
        check=True,
        text=True,
        timeout=120,
    )
    report = json.loads(completed.stdout)
    origins = {
        "interpreter": report.get("executable"),
        "prefix": report.get("prefix"),
        "base_prefix": report.get("base_prefix"),
        **{
            f"module:{name}": report.get("modules", {}).get(name)
            for name in PROBE_MODULES
        },
    }
    missing = sorted(name for name, origin in origins.items() if not origin)
    escaped = {
        name: origin
        for name, origin in origins.items()
        if origin and not Path(origin).resolve().is_relative_to(allowed_root)
    }
    if missing or escaped:
        raise RuntimeError(
            "Bundled Pocket runtime provenance failed: "
            + json.dumps({"missing": missing, "escaped": escaped}, sort_keys=True)
        )
    if not Path(runtime_site).resolve().is_relative_to(runtime_root):
        raise RuntimeError("Pocket runtime site-packages path is inconsistent")
    return report


def _sha256(path):
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _huggingface_snapshot_inventory(cache_root):
    hub = Path(cache_root) / "hub"
    artifacts = []
    if not hub.is_dir():
        return artifacts
    for snapshot in sorted(hub.glob("models--*/snapshots/*")):
        encoded_repository = snapshot.parents[1].name.removeprefix("models--")
        owner, separator, repository_name = encoded_repository.partition("--")
        repository = f"{owner}/{repository_name}" if separator else encoded_repository
        revision = snapshot.name
        for path in sorted(snapshot.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(snapshot).as_posix()
            identity = (repository, revision, relative_path)
            artifacts.append(
                {
                    "role": _PUBLIC_POCKET_ARTIFACTS.get(identity, "unexpected"),
                    "repository": repository,
                    "revision": revision,
                    "path": relative_path,
                    "size": path.stat().st_size,
                    "sha256": _sha256(path.resolve()),
                }
            )
    return artifacts


@contextmanager
def _clean_pocket_environment(cache_root):
    names = (
        "HF_HOME",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN",
    )
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ["HF_HOME"] = str(cache_root)
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def probe_bundled_pocket_render(
    bundle_root=None,
    *,
    backend_factory=IsolatedSpeechBackend,
    temporary_directory_factory=TemporaryDirectory,
):
    bundle_root = get_bundle_root() if bundle_root is None else Path(bundle_root)
    if bundle_root is None:
        raise RuntimeError("Pocket render provenance requires a frozen bundle")
    cache_parent = get_local_data_directory()
    cache_parent.mkdir(parents=True, exist_ok=True)
    with temporary_directory_factory(
        prefix="package-self-test-", dir=cache_parent
    ) as directory:
        root = Path(directory).resolve()
        hf_cache = root / "huggingface"
        with _clean_pocket_environment(hf_cache):
            backend = backend_factory(
                "pocket-tts",
                CharacterVoiceRegistry(),
                narrator_reference="alba",
                voice_state_cache_directory=root / "voices",
                persistent_audio_cache_directory=root / "audio",
                allow_gated_model_access=False,
            )
            try:
                result = backend.render(
                    SynthesisRequest(
                        voice="Narrator",
                        text="The packaged speech runtime is ready.",
                        generation_profile="default",
                        cache_policy=SynthesisCachePolicy.BYPASS,
                    )
                ).collect()
                health = dict(backend.health or {})
            finally:
                backend.shutdown()
        samples = int(result.pcm.size)
        if (
            samples <= 0
            or result.sample_rate <= 0
            or result.completion is not SynthesisCompletion.COMPLETE
        ):
            raise RuntimeError(
                "Bundled Pocket render did not produce complete non-empty PCM"
            )
        artifacts = _huggingface_snapshot_inventory(hf_cache)
        identities = {
            (item["repository"], item["revision"], item["path"]) for item in artifacts
        }
        expected = set(_PUBLIC_POCKET_ARTIFACTS)
        if identities != expected:
            raise RuntimeError(
                "Bundled Pocket render used unexpected model assets: "
                + json.dumps(
                    {
                        "missing": sorted(expected - identities),
                        "unexpected": sorted(identities - expected),
                    },
                    sort_keys=True,
                )
            )
        return {
            "sample_rate": result.sample_rate,
            "samples": samples,
            "completion": result.completion.value,
            "health": health,
            "artifacts": artifacts,
        }


def run_package_self_test(
    report_path=None,
    *,
    import_module=None,
    tesseract_probe=None,
    espeak_probe=None,
    speech_runtime_probe=None,
    speech_render_probe=None,
):
    import_module = import_module or importlib.import_module
    tesseract_probe = tesseract_probe or probe_tesseract
    espeak_probe = espeak_probe or probe_espeak
    speech_runtime_probe = speech_runtime_probe or probe_bundled_pocket_runtime
    speech_render_probe = speech_render_probe or probe_bundled_pocket_render
    bundled_tesseract = configure_bundled_dependencies()
    bundled_espeak = find_bundled_espeak()
    checks = []

    for module_name in required_modules:
        try:
            import_module(module_name)
        except Exception as error:
            checks.append(
                {
                    "name": f"Import {module_name}",
                    "status": "error",
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                }
            )
        else:
            checks.append(
                {
                    "name": f"Import {module_name}",
                    "status": "ok",
                    "message": "available",
                }
            )

    try:
        version = str(tesseract_probe())
    except Exception as error:
        checks.append(
            {
                "name": "Tesseract OCR",
                "status": "error",
                "message": str(error),
            }
        )
    else:
        checks.append(
            {
                "name": "Tesseract OCR",
                "status": "ok",
                "message": version,
            }
        )

    frozen = bool(getattr(sys, "frozen", False))
    if frozen and bundled_tesseract is None:
        checks.append(
            {
                "name": "Bundled Tesseract",
                "status": "error",
                "message": "Bundled Tesseract executable or English language data is missing",
            }
        )
    elif frozen:
        checks.append(
            {
                "name": "Bundled Tesseract",
                "status": "ok",
                "message": str(bundled_tesseract),
            }
        )
    if frozen and bundled_espeak is None:
        checks.append(
            {
                "name": "Bundled eSpeak-NG",
                "status": "error",
                "message": "Bundled eSpeak-NG executable or voice data is missing",
            }
        )
    elif frozen:
        try:
            espeak_version = espeak_probe(bundled_espeak[0])
        except Exception as error:
            checks.append(
                {
                    "name": "Bundled eSpeak-NG",
                    "status": "error",
                    "message": str(error),
                }
            )
        else:
            checks.append(
                {
                    "name": "Bundled eSpeak-NG",
                    "status": "ok",
                    "message": espeak_version,
                }
            )

    if frozen:
        try:
            runtime_report = speech_runtime_probe()
        except Exception as error:
            checks.append(
                {
                    "name": "Bundled Pocket TTS runtime",
                    "status": "error",
                    "message": str(error),
                }
            )
        else:
            checks.append(
                {
                    "name": "Bundled Pocket TTS runtime",
                    "status": "ok",
                    "message": (
                        f"{runtime_report['executable']}; "
                        f"{len(runtime_report['modules'])} modules contained"
                    ),
                    "details": runtime_report,
                }
            )
        try:
            render_report = speech_render_probe()
        except Exception as error:
            checks.append(
                {
                    "name": "Bundled Pocket TTS clean-cache render",
                    "status": "error",
                    "message": str(error),
                }
            )
        else:
            checks.append(
                {
                    "name": "Bundled Pocket TTS clean-cache render",
                    "status": "ok",
                    "message": (
                        f"{render_report['samples']} samples at "
                        f"{render_report['sample_rate']} Hz"
                    ),
                    "details": render_report,
                }
            )

    successful = all(check["status"] == "ok" for check in checks)
    report = {
        "success": successful,
        "frozen": frozen,
        "python_executable": str(Path(sys.executable).resolve()),
        "bundle_root": str(get_bundle_root() or ""),
        "checks": checks,
    }
    report_path = (
        get_local_data_directory() / "package-self-test.json"
        if report_path is None
        else Path(report_path).expanduser()
    )
    atomic_write_json(report_path, report)
    return CLIReportResult(successful, report_path)
