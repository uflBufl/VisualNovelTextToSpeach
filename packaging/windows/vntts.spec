import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPEC).resolve().parents[2]
sys.path.insert(0, str(project_root / "packaging" / "pyinstaller"))
from dependency_collection import collect_packaged_dependencies

tesseract_directory = Path(os.environ["VNTTS_TESSERACT_DIR"]).resolve()
espeak_directory = Path(os.environ["VNTTS_ESPEAK_DIR"]).resolve()
speech_runtimes_directory = Path(os.environ["VNTTS_SPEECH_RUNTIMES_DIR"]).resolve()
decoder_directory = Path(os.environ["VNTTS_VGMSTREAM_DIR"]).resolve()
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
    speech_runtimes_directory / "pocket-tts" / "python.exe",
    speech_runtimes_directory / "runtime-manifest.json",
    decoder_directory / "vgmstream-cli.exe",
    decoder_directory / "licenses" / "COPYING",
):
    if not required_path.is_file():
        raise SystemExit(f"Required speech runtime file is missing: {required_path}")

datas = [(str(english_language_data), "tesseract/tessdata")]
datas.append((str(decoder_directory / "licenses"), "vgmstream/licenses"))
datas.append((str(speech_runtimes_directory), "speech-runtimes"))
datas.extend(
    (str(source), str(Path("espeak-ng") / source.relative_to(espeak_directory).parent))
    for source in espeak_directory.rglob("*")
    if source.is_file()
)
binaries = [(str(tesseract_executable), "tesseract")]
binaries.extend(
    (str(source), "vgmstream") for source in decoder_directory.iterdir()
    if source.suffix.casefold() in {".exe", ".dll"}
)
binaries.extend(
    (str(library), "tesseract") for library in tesseract_directory.glob("*.dll")
)
hidden_imports = collect_submodules("transformers.models.gpt2")

collect_packaged_dependencies(datas, binaries, hidden_imports)

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
