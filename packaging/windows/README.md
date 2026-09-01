# Windows portable build

PyInstaller one-folder mode is used because the application includes large
Torch and Coqui libraries plus external OCR and phonemizer processes. Build it
on 64-bit Windows with Python 3.11, uv, Tesseract 5 with English language data,
and eSpeak-NG:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

Pass `-TesseractDirectory` when Tesseract is installed elsewhere. The output is
`dist\VisualNovelTextToSpeech-windows-x64.zip`. The bundle contains Python, Qt,
all Python dependencies, Tesseract, English OCR data, and eSpeak-NG; speech
models remain user-managed downloads.

On a clean Windows machine, unpack the archive and verify it with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-windows-bundle.ps1 `
  -BundleDirectory C:\path\to\VisualNovelTextToSpeech
```

Build a per-user Inno Setup installer from that bundle with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows-installer.ps1
```

The installer adds a Start Menu shortcut, offers an optional startup shortcut,
upgrades the stable application ID in place, and preserves settings, downloaded
models, and imported voices in the application-data directories. Verify an
install, real in-place upgrade, and uninstall with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-windows-installer.ps1 `
  -InstallerPath .\dist\VisualNovelTextToSpeech-0.1.0-windows-x64-setup.exe `
  -PreviousInstallerPath C:\path\to\VisualNovelTextToSpeech-previous-windows-x64-setup.exe `
  -AllowUnsigned
```

Public builds must set `VNTTS_SIGNING_CERTIFICATE_PATH` and optionally
`VNTTS_SIGNING_CERTIFICATE_PASSWORD` and `VNTTS_SIGNING_TIMESTAMP_URL`, then
pass `-RequireSignature` to the installer build script. The Windows installer
workflow accepts the certificate through the
`WINDOWS_SIGNING_CERTIFICATE_BASE64` repository secret.

Run every profile in `release-matrix.json` on an interactive Windows 11 machine
with the required GPU, monitors, and DPI scaling. The release test opens a
windowed or borderless visual-novel fixture at normal or elevated integrity,
installs the application, captures and recognizes its dialog, synthesizes and
plays the recognized text, verifies upgrade and uninstall, and writes a JSON
evidence report:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-windows-release-test.ps1 `
  -InstallerPath .\dist\VisualNovelTextToSpeech-0.1.0-windows-x64-setup.exe `
  -PreviousInstallerPath C:\path\to\VisualNovelTextToSpeech-previous-windows-x64-setup.exe `
  -ProfileName nvidia-borderless-normal-125-multidisplay `
  -ExpectedGpuVendor NVIDIA `
  -CaptureMode borderless `
  -GameProcessLevel normal `
  -MinimumDisplayCount 2 `
  -ExpectedDpiScale 125
```

The Windows release compatibility workflow runs the same check on an
interactive, self-hosted Windows 11 runner and uploads the evidence report.
After collecting every required report, enforce the release gate with:

```powershell
uv run python scripts\verify_windows_release_matrix.py `
  --evidence-directory .\release-evidence
```
