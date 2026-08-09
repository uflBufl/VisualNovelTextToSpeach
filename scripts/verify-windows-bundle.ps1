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
