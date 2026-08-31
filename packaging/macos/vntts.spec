import importlib.metadata
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata


project_root = Path(SPEC).resolve().parents[2]
tesseract_directory = Path(os.environ["VNTTS_TESSERACT_DIR"]).resolve()
espeak_directory = Path(os.environ["VNTTS_ESPEAK_DIR"]).resolve()
speech_runtimes_directory = Path(os.environ["VNTTS_SPEECH_RUNTIMES_DIR"]).resolve()
codesign_identity = os.environ.get("VNTTS_CODESIGN_IDENTITY") or None
entitlements_file = (
    str(project_root / "packaging" / "macos" / "entitlements.plist")
    if codesign_identity
    else None
)
target_arch = os.environ.get("VNTTS_MACOS_TARGET_ARCH") or None

tesseract_executable = tesseract_directory / "bin" / "tesseract"
english_language_data = tesseract_directory / "share" / "tessdata" / "eng.traineddata"
espeak_executable = espeak_directory / "bin" / "espeak-ng"
espeak_data_directory = espeak_directory / "share" / "espeak-ng-data"

for required_path in (
    tesseract_executable,
    english_language_data,
    espeak_executable,
    espeak_data_directory,
    speech_runtimes_directory / "pocket-tts" / "bin" / "python",
    speech_runtimes_directory / "runtime-manifest.json",
):
    if not required_path.exists():
        raise SystemExit(f"Required macOS dependency is missing: {required_path}")

datas = [(str(english_language_data), "tesseract/tessdata")]
datas.append((str(speech_runtimes_directory), "speech-runtimes"))
datas.extend(
    (
        str(source),
        str(
            Path("espeak-ng/espeak-ng-data")
            / source.relative_to(espeak_data_directory).parent
        ),
    )
    for source in espeak_data_directory.rglob("*")
    if source.is_file()
)
for source, destination in (
    (tesseract_directory / "LICENSE", "third-party-licenses/tesseract"),
    (espeak_directory / "COPYING", "third-party-licenses/espeak-ng"),
):
    if source.is_file():
        datas.append((str(source), destination))

binaries = [
    (str(tesseract_executable), "tesseract"),
    (str(espeak_executable), "espeak-ng"),
]
hidden_imports = collect_submodules("transformers.models.gpt2")
hidden_imports.extend(["ApplicationServices", "Quartz"])

for package in ("TTS", "coqpit", "gruut", "ko_speech_tools", "trainer"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hidden_imports.extend(package_imports)

for distribution in (
    "coqui-tts",
    "coqpit",
    "gruut",
    "ko-speech-tools",
    "torch",
    "torchaudio",
    "trainer",
    "transformers",
):
    try:
        datas.extend(copy_metadata(distribution))
    except Exception:
        pass

analysis = Analysis(
    [str(project_root / "vntts" / "app.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hidden_imports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[
        str(
            project_root
            / "packaging"
            / "pyinstaller"
            / "runtime_hooks"
            / "numba_cache.py"
        ),
        str(
            project_root
            / "packaging"
            / "pyinstaller"
            / "runtime_hooks"
            / "ko_speech_tools_data.py"
        ),
    ],
    excludes=["IPython", "jupyter", "matplotlib.tests", "pytest"],
    noarchive=False,
    optimize=0,
    module_collection_mode={
        "inflect": "py",
        "librosa": "py",
        "torch._dynamo.config": "py",
        "torch._functorch.config": "py",
        "torch._inductor.config": "py",
        "torch.compiler.config": "py",
        "torch.fx.experimental._config": "py",
    },
)
python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="VisualNovelTextToSpeech",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=codesign_identity,
    entitlements_file=entitlements_file,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VisualNovelTextToSpeech",
)

application = BUNDLE(
    collection,
    name="Visual Novel Text to Speech.app",
    bundle_identifier="io.github.visualnoveltexttospeech.app",
    version=importlib.metadata.version("visual-novel-text-to-speech"),
    info_plist={
        "CFBundleDisplayName": "Visual Novel Text to Speech",
        "CFBundleName": "Visual Novel Text to Speech",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
