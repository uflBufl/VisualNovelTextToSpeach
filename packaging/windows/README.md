# Windows portable build

PyInstaller one-folder mode is used because the application includes large
Torch and Coqui libraries plus external OCR and phonemizer processes. Build it
on 64-bit Windows 10 version 2004 (build 19041) or newer with Python 3.14, uv,
Rust/Cargo using the MSVC toolchain, Visual Studio C++ Build Tools and Windows
SDK, Tesseract 5 with English language data, and eSpeak-NG. The build compiles
and tests the native Windows Graphics Capture helper:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

The test gate works from an ordinary Windows account. Security tests that need
to create symbolic links are skipped only when Windows returns error 1314; turn
on Developer Mode or run elevated if those containment tests must also execute.
Other filesystem errors remain failures.

Blind-review keys still require regular files, reject symbolic links, and stay
checksum-bound. Windows uses the current user's profile-directory ACL instead
of comparing Unix `0600` mode bits, which Windows does not implement.

Cross-platform tests preserve foreign platform paths as lexical values. An
explicit macOS `/Applications/...` launch argument must not be resolved as a
Windows path, while relative artifact paths remain POSIX-separated on disk.

The build discovers Tesseract from `PATH`, then the standard Program Files
locations. Pass `-TesseractDirectory` to override discovery. The output is
`dist\VisualNovelTextToSpeech-windows-x64.zip`. The bundle contains Python, Qt,
all Python dependencies, the capture helper, Tesseract, English OCR data, and
eSpeak-NG; speech models remain user-managed downloads.

On a clean Windows machine, unpack the archive and verify it with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-windows-bundle.ps1 `
  -BundleDirectory C:\path\to\VisualNovelTextToSpeech
```

Windows is distributed as this portable bundle; no installer is required.
Public builds must set `VNTTS_SIGNING_CERTIFICATE_PATH` and optionally
`VNTTS_SIGNING_CERTIFICATE_PASSWORD` and `VNTTS_SIGNING_TIMESTAMP_URL`, then
pass `-RequireSignature` to `scripts\build-windows.ps1`. Both the launcher and
capture helper are signed. The Windows workflow accepts the certificate through the
`WINDOWS_SIGNING_CERTIFICATE_BASE64` repository secret.

Run every profile in `release-matrix.json` on an interactive Windows 10 build
19041+ machine with the required GPU, monitors, and DPI scaling. The release test opens a
windowed or borderless visual-novel fixture at normal or elevated integrity,
extracts the portable bundle, captures and recognizes its dialog, synthesizes
and plays the recognized text, and writes a JSON evidence report:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-windows-release-test.ps1 `
  -BundleDirectory .\dist\windows\VisualNovelTextToSpeech `
  -BundleArchive .\dist\VisualNovelTextToSpeech-windows-x64.zip `
  -ProfileName nvidia-borderless-normal-125-multidisplay `
  -ExpectedGpuVendor NVIDIA `
  -CaptureMode borderless `
  -GameProcessLevel normal `
  -MinimumDisplayCount 2 `
  -ExpectedDpiScale 125
```

The Windows release compatibility workflow runs the same check on an
interactive, self-hosted Windows runner (build 19041+) and uploads the evidence
report.
After collecting every required report, enforce the release gate with:

```powershell
uv run python scripts\verify_windows_release_matrix.py `
  --evidence-directory .\release-evidence
```
