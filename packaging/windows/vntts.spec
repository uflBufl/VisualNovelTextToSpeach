import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata


project_root = Path(SPEC).resolve().parents[2]
tesseract_directory = Path(os.environ["VNTTS_TESSERACT_DIR"]).resolve()
espeak_directory = Path(os.environ["VNTTS_ESPEAK_DIR"]).resolve()
speech_runtimes_directory = Path(os.environ["VNTTS_SPEECH_RUNTIMES_DIR"]).resolve()
tesseract_executable = tesseract_directory / "tesseract.exe"
english_language_data = tesseract_directory / "tessdata" / "eng.traineddata"

if not tesseract_executable.is_file():
    raise SystemExit(f"Tesseract executable is missing: {tesseract_executable}")
if not english_language_data.is_file():
    raise SystemExit(f"English language data is missing: {english_language_data}")
espeak_executables = list(espeak_directory.rglob("espeak-ng.exe"))
espeak_data_directories = list(espeak_directory.rglob("espeak-ng-data"))
if not espeak_executables:
    raise SystemExit(f"eSpeak-NG executable is missing under: {espeak_directory}")
if not espeak_data_directories:
    raise SystemExit(f"eSpeak-NG voice data is missing under: {espeak_directory}")
for required_path in (
    speech_runtimes_directory / "pocket-tts" / "Scripts" / "python.exe",
    speech_runtimes_directory / "runtime-manifest.json",
):
    if not required_path.is_file():
        raise SystemExit(f"Required speech runtime file is missing: {required_path}")

datas = [(str(english_language_data), "tesseract/tessdata")]
datas.append((str(speech_runtimes_directory), "speech-runtimes"))
datas.extend(
    (str(source), str(Path("espeak-ng") / source.relative_to(espeak_directory).parent))
    for source in espeak_directory.rglob("*")
    if source.is_file()
)
binaries = [(str(tesseract_executable), "tesseract")]
binaries.extend(
    (str(library), "tesseract") for library in tesseract_directory.glob("*.dll")
)
hidden_imports = collect_submodules("transformers.models.gpt2")

provider_datas, provider_binaries, provider_imports = collect_all("r1999extractor")
datas.extend(provider_datas)
binaries.extend(provider_binaries)
hidden_imports.extend(provider_imports)

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
    "reverse1999-extractor",
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
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VisualNovelTextToSpeech",
)
