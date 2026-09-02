param(
    [string]$Character = "Rhiannon",
    [string]$Text = "The tide is turning. We should return before the storm arrives.",
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "Chatterbox Nano Windows qualification must run on Windows."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $RunId = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $OutputDirectory = Join-Path $ProjectRoot `
        "build\windows\chatterbox-nano-$RunId"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$Manifest = Join-Path $ProjectRoot "data\reverse1999-voices\manifest.json"

Push-Location $ProjectRoot
try {
    uv sync --project backends/chatterbox-nano --frozen
    if ($LASTEXITCODE -ne 0) {
        throw "Chatterbox Nano runtime installation failed."
    }

    uv run --frozen vntts-benchmark-tts `
        --backend chatterbox-nano `
        --character $Character `
        --text $Text `
        --manifest $Manifest `
        --output $OutputDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Chatterbox Nano worker/render qualification failed."
    }

    $ReportPath = Join-Path $OutputDirectory "chatterbox-nano.json"
    $Report = Get-Content $ReportPath -Raw | ConvertFrom-Json
    $Samples = @($Report.samples)
    if ($Report.backend -ne "chatterbox-nano" -or $Samples.Count -ne 1) {
        throw "Chatterbox Nano qualification report is invalid."
    }
    $AudioPath = [string]$Samples[0].audio
    if (-not (Test-Path $AudioPath -PathType Leaf)) {
        throw "Chatterbox Nano qualification did not publish its WAV."
    }

    Write-Host "Chatterbox Nano qualification passed."
    Write-Host "Listen to: $AudioPath"
    Write-Host "Report: $ReportPath"
}
finally {
    Pop-Location
}
