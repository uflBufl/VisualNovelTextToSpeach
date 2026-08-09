param(
    [Parameter(Mandatory = $true)]
    [string]$BundleDirectory
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
$OriginalPath = $env:PATH
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
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
    $env:PATH = $OriginalPath
}

if (Test-Path $ReportPath) {
    Get-Content $ReportPath
}
if ($SelfTest.ExitCode -ne 0) {
    throw "The standalone package self-test failed."
}
Write-Host "Standalone package self-test passed without development tools on PATH."
