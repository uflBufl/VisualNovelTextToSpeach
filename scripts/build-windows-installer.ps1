param(
    [string]$BundleDirectory,
    [string]$Version = "0.1.0",
    [string]$OutputDirectory,
    [string]$InnoSetupCompiler,
    [switch]$Sign,
    [switch]$RequireSignature
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "The Windows installer must be built on Windows."
}
if ($Version -notmatch "^\d+\.\d+\.\d+(\.\d+)?$") {
    throw "Version must contain three or four numeric components."
}
$FileVersion = if (($Version -split "\.").Count -eq 3) {
    "$Version.0"
}
else {
    $Version
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $BundleDirectory) {
    $BundleDirectory = Join-Path $ProjectRoot `
        "dist\windows\VisualNovelTextToSpeech"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $ProjectRoot "dist"
}
$BundleDirectory = (Resolve-Path $BundleDirectory).Path
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$Executable = Join-Path $BundleDirectory "VisualNovelTextToSpeech.exe"
if (-not (Test-Path $Executable -PathType Leaf)) {
    throw "Portable application is missing: $Executable"
}

if (-not $InnoSetupCompiler) {
    $InnoSetupCompiler = Join-Path ${env:ProgramFiles(x86)} `
        "Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path $InnoSetupCompiler -PathType Leaf)) {
    throw "Inno Setup compiler is missing: $InnoSetupCompiler"
}

$SigningConfigured = [bool](
    $env:VNTTS_SIGNING_CERTIFICATE_PATH -or
    $env:VNTTS_SIGNING_CERTIFICATE_THUMBPRINT
)
if ($RequireSignature -and -not $SigningConfigured) {
    throw "A signed release requires an Authenticode certificate."
}
if ($Sign -or $RequireSignature) {
    & (Join-Path $PSScriptRoot "sign-windows.ps1") -Files $Executable
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$InstallerScript = Join-Path $ProjectRoot "packaging\windows\installer.iss"
& $InnoSetupCompiler `
    "/DVNTTS_BUNDLE_DIR=$BundleDirectory" `
    "/DVNTTS_OUTPUT_DIR=$OutputDirectory" `
    "/DVNTTS_VERSION=$Version" `
    "/DVNTTS_FILE_VERSION=$FileVersion" `
    $InstallerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed."
}

$Installer = Join-Path $OutputDirectory `
    "VisualNovelTextToSpeech-$Version-windows-x64-setup.exe"
if (-not (Test-Path $Installer -PathType Leaf)) {
    throw "Installer was not created: $Installer"
}
if ($Sign -or $RequireSignature) {
    & (Join-Path $PSScriptRoot "sign-windows.ps1") -Files $Installer
}
else {
    Write-Warning "Built an unsigned development installer."
}
Write-Host "Windows installer: $Installer"
