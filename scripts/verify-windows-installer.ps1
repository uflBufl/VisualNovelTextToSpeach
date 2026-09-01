param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [switch]$AllowUnsigned,
    [string]$SmokeTestImage,
    [string]$SmokeTestWindowTitle,
    [string]$SmokeTestModel = "tts_models/en/vctk/vits",
    [string]$ExpectedSpeaker,
    [switch]$VerifyAutoAdvance,
    [switch]$ElevatedSmokeTest
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
    $InstalledExecutable = Join-Path $InstallDirectory `
        "VisualNovelTextToSpeech.exe"
    if (-not $AllowUnsigned) {
        $ExecutableSignature = Get-AuthenticodeSignature $InstalledExecutable
        if ($ExecutableSignature.Status -ne "Valid") {
            throw "Installed executable signature is not valid: " +
                $ExecutableSignature.Status
        }
    }
    if ($SmokeTestImage -or $SmokeTestWindowTitle) {
        if ($SmokeTestImage -and $SmokeTestWindowTitle) {
            throw "Choose either a smoke-test image or a window title."
        }
        $SmokeReport = Join-Path $env:TEMP `
            "vntts-release-smoke-$VerificationId.json"
        $SmokeArguments = @(
            "--release-smoke-test-report",
            ('"{0}"' -f $SmokeReport),
            "--release-smoke-test-model",
            ('"{0}"' -f $SmokeTestModel)
        )
        if ($SmokeTestImage) {
            $SmokeArguments += @(
                "--release-smoke-test-image",
                ('"{0}"' -f (Resolve-Path $SmokeTestImage).Path)
            )
        }
        else {
            $SmokeArguments += @(
                "--release-smoke-test-window-title",
                ('"{0}"' -f $SmokeTestWindowTitle)
            )
        }
        if ($ExpectedSpeaker) {
            $SmokeArguments += @(
                "--release-smoke-test-expected-speaker",
                ('"{0}"' -f $ExpectedSpeaker)
            )
        }
        if ($VerifyAutoAdvance) {
            if (-not $SmokeTestWindowTitle) {
                throw "Auto-advance verification requires a smoke-test window."
            }
            $SmokeArguments += @(
                "--release-smoke-test-auto-advance-expected-text",
                '"Auto advance acknowledged."'
            )
        }
        $SmokeParameters = @{
            FilePath = $InstalledExecutable
            ArgumentList = $SmokeArguments
            Wait = $true
            PassThru = $true
        }
        if ($ElevatedSmokeTest) {
            $SmokeParameters.Verb = "RunAs"
        }
        $Smoke = Start-Process @SmokeParameters
        if (Test-Path $SmokeReport -PathType Leaf) {
            Get-Content $SmokeReport
        }
        if ($Smoke.ExitCode -ne 0) {
            throw "Installed OCR-to-speech smoke test failed."
        }
        if ($VerifyAutoAdvance) {
            if (-not (Test-Path $SmokeReport -PathType Leaf)) {
                throw "Installed auto-advance smoke report is missing."
            }
            $SmokeEvidence = Get-Content $SmokeReport -Raw | ConvertFrom-Json
            if (
                $SmokeEvidence.auto_advance_dispatched -ne $true -or
                $SmokeEvidence.auto_advance_acknowledged -ne $true -or
                $SmokeEvidence.auto_advance_controller -ne
                    "AppController._auto_advance_dialog"
            ) {
                throw "Installed production auto advance was not acknowledged."
            }
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
