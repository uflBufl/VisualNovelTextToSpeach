# Windows portable build

PyInstaller one-folder mode is used because the application includes large
Torch and Coqui libraries plus an external Tesseract process. Build it on
64-bit Windows with Python 3.11, uv, and a Tesseract 5 installation containing
English language data:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

Pass `-TesseractDirectory` when Tesseract is installed elsewhere. The output is
`dist\VisualNovelTextToSpeech-windows-x64.zip`. The bundle contains Python, Qt,
all Python dependencies, `tesseract.exe`, its DLLs, and
`tessdata\eng.traineddata`; speech models remain user-managed downloads.

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
install, in-place upgrade, and uninstall with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-windows-installer.ps1 `
  -InstallerPath .\dist\VisualNovelTextToSpeech-0.1.0-windows-x64-setup.exe `
  -AllowUnsigned
```

Public builds must set `VNTTS_SIGNING_CERTIFICATE_PATH` and optionally
`VNTTS_SIGNING_CERTIFICATE_PASSWORD` and `VNTTS_SIGNING_TIMESTAMP_URL`, then
pass `-RequireSignature` to the installer build script. The Windows installer
workflow accepts the certificate through the
`WINDOWS_SIGNING_CERTIFICATE_BASE64` repository secret.
