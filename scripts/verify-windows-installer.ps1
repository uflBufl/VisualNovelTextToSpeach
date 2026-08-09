param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [switch]$AllowUnsigned
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "The Windows installer must be verified on Windows."
}

$InstallerPath = (Resolve-Path $InstallerPath).Path
$Signature = Get-AuthenticodeSignature $InstallerPath
if (-not $AllowUnsigned -and $Signature.Status -ne "Valid") {
    throw "Installer signature is not valid: $($Signature.Status)"
}

$VerificationId = [guid]::NewGuid().ToString("N")
$InstallDirectory = Join-Path $env:TEMP "vntts-install-$VerificationId"
$LocalDataDirectory = Join-Path $env:LOCALAPPDATA "VisualNovelTextToSpeech"
$Marker = Join-Path $LocalDataDirectory ".installer-verification-$VerificationId"
$StartMenuShortcut = Join-Path $env:APPDATA `
    "Microsoft\Windows\Start Menu\Programs\Visual Novel Text to Speech\Visual Novel Text to Speech.lnk"
$StartupShortcut = Join-Path $env:APPDATA `
    "Microsoft\Windows\Start Menu\Programs\Startup\Visual Novel Text to Speech.lnk"

try {
    New-Item -ItemType Directory -Path $LocalDataDirectory -Force | Out-Null
    Set-Content -Path $Marker -Value "preserve during upgrade and uninstall"

    $Install = Start-Process -FilePath $InstallerPath `
        -ArgumentList @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/CURRENTUSER",
            "/TASKS=startup",
            ('/DIR="{0}"' -f $InstallDirectory)
        ) `
        -Wait `
        -PassThru
    if ($Install.ExitCode -ne 0) {
        throw "Installer exited with code $($Install.ExitCode)."
    }

    & (Join-Path $PSScriptRoot "verify-windows-bundle.ps1") `
        -BundleDirectory $InstallDirectory
    if (-not $AllowUnsigned) {
        $InstalledExecutable = Join-Path $InstallDirectory `
            "VisualNovelTextToSpeech.exe"
        $ExecutableSignature = Get-AuthenticodeSignature $InstalledExecutable
        if ($ExecutableSignature.Status -ne "Valid") {
            throw "Installed executable signature is not valid: " +
                $ExecutableSignature.Status
        }
    }
    if (-not (Test-Path $StartMenuShortcut -PathType Leaf)) {
        throw "Start Menu shortcut was not created."
    }
    if (-not (Test-Path $StartupShortcut -PathType Leaf)) {
        throw "Optional startup shortcut was not created."
    }

    $Upgrade = Start-Process -FilePath $InstallerPath `
        -ArgumentList @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/CURRENTUSER",
            ('/DIR="{0}"' -f $InstallDirectory)
        ) `
        -Wait `
        -PassThru
    if ($Upgrade.ExitCode -ne 0 -or -not (Test-Path $Marker -PathType Leaf)) {
        throw "Upgrade did not preserve application data."
    }

    $Uninstaller = Join-Path $InstallDirectory "unins000.exe"
    $Uninstall = Start-Process -FilePath $Uninstaller `
        -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
        -Wait `
        -PassThru
    if ($Uninstall.ExitCode -ne 0) {
        throw "Uninstaller exited with code $($Uninstall.ExitCode)."
    }
    if (Test-Path $InstallDirectory) {
        throw "Uninstaller left the installation directory behind."
    }
    if (Test-Path $StartMenuShortcut -PathType Leaf) {
        throw "Uninstaller left the Start Menu shortcut behind."
    }
    if (Test-Path $StartupShortcut -PathType Leaf) {
        throw "Uninstaller left the startup shortcut behind."
    }
    if (-not (Test-Path $Marker -PathType Leaf)) {
        throw "Uninstaller removed downloaded models or user data."
    }
    Write-Host "Installer, upgrade, data preservation, and uninstall checks passed."
}
finally {
    Remove-Item $Marker -Force -ErrorAction SilentlyContinue
}
