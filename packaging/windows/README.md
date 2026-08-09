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
