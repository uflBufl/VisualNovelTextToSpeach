param(
    [Parameter(Mandatory = $true)]
    [string]$BundleDirectory,
    [string]$SmokeTestWindowTitle,
    [string]$SmokeTestModel = "tts_models/en/vctk/vits",
    [string]$ExpectedSpeaker,
    [string]$SmokeEvidenceReport,
    [switch]$VerifyAutoAdvance,
    [switch]$ElevatedSmokeTest
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$BundleDirectory = (Resolve-Path $BundleDirectory).Path
$Executable = Join-Path $BundleDirectory "VisualNovelTextToSpeech.exe"
if (-not (Test-Path $Executable -PathType Leaf)) {
    throw "Packaged executable is missing: $Executable"
}
$BundledTesseract = Join-Path $BundleDirectory `
    "_internal\tesseract\tesseract.exe"
$BundledEnglishData = Join-Path $BundleDirectory `
    "_internal\tesseract\tessdata\eng.traineddata"
$BundledEspeak = Get-ChildItem `
    (Join-Path $BundleDirectory "_internal\espeak-ng") `
    -Filter "espeak-ng.exe" -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1
$BundledEspeakData = Get-ChildItem `
    (Join-Path $BundleDirectory "_internal\espeak-ng") `
    -Directory -Filter "espeak-ng-data" -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not (Test-Path $BundledTesseract -PathType Leaf)) {
    throw "Bundled Tesseract executable is missing."
}
if (-not (Test-Path $BundledEnglishData -PathType Leaf)) {
    throw "Bundled English Tesseract data is missing."
}
if (-not $BundledEspeak -or -not $BundledEspeakData) {
    throw "Bundled eSpeak-NG executable or voice data is missing."
}

$ReportPath = Join-Path $env:TEMP "vntts-package-self-test.json"
$EnvironmentNames = @(
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
    "TESSDATA_PREFIX",
    "ESPEAK_DATA_PATH"
    "VNTTS_POCKET_TTS_RUNTIME"
    "VNTTS_CHATTERBOX_RUNTIME"
    "VNTTS_MOSS_RUNTIME"
    "VNTTS_MOSS_DELAY_RUNTIME"
    "HF_HOME"
    "HF_HUB_CACHE"
    "HUGGINGFACE_HUB_CACHE"
    "TRANSFORMERS_CACHE"
    "HF_TOKEN"
    "HUGGING_FACE_HUB_TOKEN"
)
$OriginalEnvironment = @{}
foreach ($Name in $EnvironmentNames) {
    $OriginalEnvironment[$Name] = [Environment]::GetEnvironmentVariable(
        $Name,
        "Process"
    )
}
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:PYTHONHOME = $null
    $env:PYTHONPATH = $null
    $env:VIRTUAL_ENV = $null
    $env:UV_PROJECT_ENVIRONMENT = $null
    $env:TESSDATA_PREFIX = $null
    $env:ESPEAK_DATA_PATH = $null
    $env:VNTTS_POCKET_TTS_RUNTIME = $null
    $env:VNTTS_CHATTERBOX_RUNTIME = $null
    $env:VNTTS_MOSS_RUNTIME = $null
    $env:VNTTS_MOSS_DELAY_RUNTIME = $null
    $env:HF_HOME = $null
    $env:HF_HUB_CACHE = $null
    $env:HUGGINGFACE_HUB_CACHE = $null
    $env:TRANSFORMERS_CACHE = $null
    $env:HF_TOKEN = $null
    $env:HUGGING_FACE_HUB_TOKEN = $null
    Remove-Item $ReportPath -Force -ErrorAction SilentlyContinue
    $SelfTest = Start-Process -FilePath $Executable `
        -ArgumentList @(
            "--package-self-test",
            "--package-self-test-report",
            ('"{0}"' -f $ReportPath)
        ) `
        -Wait `
        -PassThru
}
finally {
    foreach ($Name in $EnvironmentNames) {
        [Environment]::SetEnvironmentVariable(
            $Name,
            $OriginalEnvironment[$Name],
            "Process"
        )
    }
}

if (-not (Test-Path $ReportPath -PathType Leaf)) {
    throw "The standalone package did not create a self-test report."
}
$Report = Get-Content $ReportPath -Raw | ConvertFrom-Json
$Report | ConvertTo-Json -Depth 5
if ($SelfTest.ExitCode -ne 0) {
    throw "The standalone package self-test failed."
}
if ($Report.success -ne $true -or $Report.frozen -ne $true) {
    throw "The self-test did not run successfully from a frozen Python runtime."
}
$PythonExecutable = [System.IO.Path]::GetFullPath(
    [string]$Report.python_executable
)
if ($PythonExecutable -ne [System.IO.Path]::GetFullPath($Executable)) {
    throw "The self-test used an unexpected Python executable: $PythonExecutable"
}
$BundleRoot = [System.IO.Path]::GetFullPath([string]$Report.bundle_root)
$BundlePrefix = $BundleDirectory.TrimEnd("\") + "\"
if (-not $BundleRoot.StartsWith(
    $BundlePrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "The embedded Python bundle root is outside the portable directory."
}
$RequiredChecks = @(
    "Import PySide6",
    "Import PIL",
    "Import TTS.api",
    "Import TTS.tts.configs.xtts_config",
    "Import mss",
    "Import pynput",
    "Import pytesseract",
    "Import sounddevice",
    "Import torch",
    "Import torchaudio",
    "Tesseract OCR",
    "Bundled Tesseract",
    "Bundled eSpeak-NG"
    "Bundled Pocket TTS runtime"
    "Bundled Pocket TTS clean-cache render"
)
foreach ($Name in $RequiredChecks) {
    $Check = @($Report.checks | Where-Object { $_.name -eq $Name })
    if ($Check.Count -ne 1 -or $Check[0].status -ne "ok") {
        throw "Required standalone check did not pass: $Name"
    }
}
if ($SmokeTestWindowTitle) {
    $SmokeReport = if ($SmokeEvidenceReport) {
        [System.IO.Path]::GetFullPath($SmokeEvidenceReport)
    }
    else {
        Join-Path $env:TEMP "vntts-release-smoke-$([guid]::NewGuid().ToString('N')).json"
    }
    $SmokeArguments = @(
        "--release-smoke-test-report",
        ('"{0}"' -f $SmokeReport),
        "--release-smoke-test-model",
        ('"{0}"' -f $SmokeTestModel),
        "--release-smoke-test-window-title",
        ('"{0}"' -f $SmokeTestWindowTitle)
    )
    if ($ExpectedSpeaker) {
        $SmokeArguments += @(
            "--release-smoke-test-expected-speaker",
            ('"{0}"' -f $ExpectedSpeaker)
        )
    }
    if ($VerifyAutoAdvance) {
        $SmokeArguments += @(
            "--release-smoke-test-auto-advance-expected-text",
            '"Auto advance acknowledged."'
        )
    }
    $SmokeParameters = @{
        FilePath = $Executable
        ArgumentList = $SmokeArguments
        Wait = $true
        PassThru = $true
    }
    if ($ElevatedSmokeTest) {
        $SmokeParameters.Verb = "RunAs"
    }
    $Smoke = Start-Process @SmokeParameters
    if ($Smoke.ExitCode -ne 0) {
        throw "Portable OCR-to-speech smoke test failed."
    }
    if ($VerifyAutoAdvance) {
        if (-not (Test-Path $SmokeReport -PathType Leaf)) {
            throw "Portable auto-advance smoke report is missing."
        }
        $SmokeEvidence = Get-Content $SmokeReport -Raw | ConvertFrom-Json
        if (
            $SmokeEvidence.auto_advance_dispatched -ne $true -or
            $SmokeEvidence.auto_advance_acknowledged -ne $true -or
            $SmokeEvidence.auto_advance_controller -ne
                "AppController._auto_advance_dialog"
        ) {
            throw "Portable production auto advance was not acknowledged."
        }
    }
    if (-not $SmokeEvidenceReport) {
        Remove-Item $SmokeReport -Force -ErrorAction SilentlyContinue
    }
}
Write-Host "Standalone package self-test passed without development tools on PATH."
