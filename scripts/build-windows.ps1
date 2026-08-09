param(
    [string]$TesseractDirectory = "C:\Program Files\Tesseract-OCR",
    [string]$EspeakDirectory,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "The Windows bundle must be built on Windows."
}
if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "Only 64-bit Windows builds are currently supported."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TesseractDirectory = (Resolve-Path $TesseractDirectory).Path
$TesseractExecutable = Join-Path $TesseractDirectory "tesseract.exe"
$EnglishLanguageData = Join-Path $TesseractDirectory "tessdata\eng.traineddata"
if (-not (Test-Path $TesseractExecutable -PathType Leaf)) {
    throw "Tesseract executable is missing: $TesseractExecutable"
}
if (-not (Test-Path $EnglishLanguageData -PathType Leaf)) {
    throw "English language data is missing: $EnglishLanguageData"
}
if (-not $EspeakDirectory) {
    $EspeakCommand = Get-Command "espeak-ng.exe" -ErrorAction SilentlyContinue
    $EspeakCandidates = @(
        $(if ($EspeakCommand) { Split-Path -Parent $EspeakCommand.Source }),
        (Join-Path $env:ProgramFiles "eSpeak NG"),
        (Join-Path ${env:ProgramFiles(x86)} "eSpeak NG")
    ) | Where-Object { $_ -and (Test-Path $_ -PathType Container) }
    $EspeakDirectory = $EspeakCandidates | Select-Object -First 1
}
if (-not $EspeakDirectory) {
    throw "eSpeak-NG installation was not found."
}
$EspeakDirectory = (Resolve-Path $EspeakDirectory).Path
$EspeakExecutable = Get-ChildItem $EspeakDirectory -Filter "espeak-ng.exe" `
    -Recurse | Select-Object -First 1
$EspeakData = Get-ChildItem $EspeakDirectory -Directory `
    -Filter "espeak-ng-data" -Recurse | Select-Object -First 1
if (-not $EspeakExecutable) {
    throw "eSpeak-NG executable is missing under: $EspeakDirectory"
}
if (-not $EspeakData) {
    throw "eSpeak-NG voice data is missing under: $EspeakDirectory"
}

Push-Location $ProjectRoot
try {
    uv sync --group dev --frozen
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync failed."
    }
    if (-not $SkipTests) {
        uv run --frozen python -m unittest discover -s tests
        if ($LASTEXITCODE -ne 0) {
            throw "Tests failed."
        }
    }

    $env:VNTTS_TESSERACT_DIR = $TesseractDirectory
    $env:VNTTS_ESPEAK_DIR = $EspeakDirectory
    $WorkPath = Join-Path $ProjectRoot "build\windows\pyinstaller"
    $DistPath = Join-Path $ProjectRoot "dist\windows"
    uv run --frozen pyinstaller --noconfirm --clean `
        --workpath $WorkPath `
        --distpath $DistPath `
        "packaging\windows\vntts.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed."
    }

    $Executable = Join-Path $DistPath `
        "VisualNovelTextToSpeech\VisualNovelTextToSpeech.exe"
    $ReportPath = Join-Path $ProjectRoot "build\windows\package-self-test.json"
    $SelfTest = Start-Process -FilePath $Executable `
        -ArgumentList @(
            "--package-self-test",
            "--package-self-test-report",
            ('"{0}"' -f $ReportPath)
        ) `
        -Wait `
        -PassThru
    if ($SelfTest.ExitCode -ne 0) {
        if (Test-Path $ReportPath) {
            Get-Content $ReportPath
        }
        throw "The packaged application self-test failed."
    }
    $PublishedReport = Join-Path $ProjectRoot `
        "dist\VisualNovelTextToSpeech-windows-x64-self-test.json"
    Copy-Item $ReportPath $PublishedReport -Force

    $Archive = Join-Path $ProjectRoot `
        "dist\VisualNovelTextToSpeech-windows-x64.zip"
    Compress-Archive `
        -Path (Join-Path $DistPath "VisualNovelTextToSpeech") `
        -DestinationPath $Archive `
        -CompressionLevel Optimal `
        -Force
    $ChecksumPath = "$Archive.sha256"
    $Checksum = (Get-FileHash $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -Path $ChecksumPath `
        -Value "$Checksum  $([System.IO.Path]::GetFileName($Archive))"
    Write-Host "Windows bundle: $Archive"
    Write-Host "SHA256 checksum: $ChecksumPath"
    Write-Host "Self-test report: $ReportPath"
}
finally {
    Pop-Location
}
