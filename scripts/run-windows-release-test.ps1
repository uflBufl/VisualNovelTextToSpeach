param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$PreviousInstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$ProfileName,
    [Parameter(Mandatory = $true)]
    [ValidateSet("Intel", "NVIDIA", "AMD")]
    [string]$ExpectedGpuVendor,
    [ValidateSet("windowed", "borderless")]
    [string]$CaptureMode = "windowed",
    [ValidateSet("normal", "elevated")]
    [string]$GameProcessLevel = "normal",
    [int]$MinimumDisplayCount = 1,
    [int]$ExpectedDpiScale = 0,
    [int]$MonitorIndex = 0,
    [string]$SmokeTestModel = "tts_models/en/vctk/vits",
    [string]$EvidenceReport,
    [switch]$AllowUnsigned
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "Windows release qualification must run on Windows."
}
if ($MinimumDisplayCount -lt 1) {
    throw "MinimumDisplayCount must be positive."
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$InstallerPath = (Resolve-Path $InstallerPath).Path
$PreviousInstallerPath = (Resolve-Path $PreviousInstallerPath).Path
if (-not $EvidenceReport) {
    $EvidenceReport = Join-Path $ProjectRoot "build\windows\release-evidence.json"
}
$EvidenceReport = [System.IO.Path]::GetFullPath($EvidenceReport)
$EvidenceDirectory = Split-Path -Parent $EvidenceReport
New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
$SmokeEvidenceReport = Join-Path $EvidenceDirectory "installed-smoke-evidence.json"
Remove-Item $SmokeEvidenceReport -Force -ErrorAction SilentlyContinue

$OperatingSystem = Get-CimInstance Win32_OperatingSystem
$BuildNumber = [int]$OperatingSystem.BuildNumber
if ($OperatingSystem.Caption -notmatch "Windows 11" -or $BuildNumber -lt 22000) {
    throw "Release qualification requires Windows 11, got $($OperatingSystem.Caption)."
}

$VideoControllers = @(Get-CimInstance Win32_VideoController)
$GpuNames = @($VideoControllers | ForEach-Object { $_.Name })
$GpuPattern = switch ($ExpectedGpuVendor) {
    "Intel" { "Intel" }
    "NVIDIA" { "NVIDIA" }
    "AMD" { "AMD|Radeon" }
}
if (-not ($GpuNames -match $GpuPattern)) {
    throw "Expected a $ExpectedGpuVendor GPU, found: $($GpuNames -join ', ')."
}

Add-Type -AssemblyName System.Windows.Forms
$Displays = [System.Windows.Forms.Screen]::AllScreens
if ($Displays.Count -lt $MinimumDisplayCount) {
    throw "Expected at least $MinimumDisplayCount displays, found $($Displays.Count)."
}
if ($MonitorIndex -lt 0 -or $MonitorIndex -ge $Displays.Count) {
    throw "Monitor index $MonitorIndex is not available."
}
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class VnttsReleaseDpi {
    [StructLayout(LayoutKind.Sequential)]
    public struct Point { public int X; public int Y; }
    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromPoint(Point point, uint flags);
    [DllImport("shcore.dll")]
    private static extern int GetDpiForMonitor(
        IntPtr monitor, int dpiType, out uint dpiX, out uint dpiY
    );
    public static uint GetEffectiveDpi(int x, int y) {
        Point point = new Point { X = x, Y = y };
        IntPtr monitor = MonitorFromPoint(point, 2);
        uint dpiX;
        uint dpiY;
        int result = GetDpiForMonitor(monitor, 0, out dpiX, out dpiY);
        if (result != 0) throw new InvalidOperationException(
            "GetDpiForMonitor failed with HRESULT " + result
        );
        return dpiX;
    }
}
"@
$SelectedDisplay = $Displays[$MonitorIndex]
$MonitorDpi = [VnttsReleaseDpi]::GetEffectiveDpi(
    ($SelectedDisplay.Bounds.Left + 1),
    ($SelectedDisplay.Bounds.Top + 1)
)
$DpiScale = [int][Math]::Round(($MonitorDpi / 96.0) * 100)
if ($ExpectedDpiScale -and $DpiScale -ne $ExpectedDpiScale) {
    throw "Expected $ExpectedDpiScale% DPI scaling, found $DpiScale%."
}

$TestId = [guid]::NewGuid().ToString("N")
$WindowTitle = "VNTTS capture fixture $TestId"
$ReadyFile = Join-Path $env:TEMP "vntts-fixture-ready-$TestId"
$StopFile = Join-Path $env:TEMP "vntts-fixture-stop-$TestId"
$FixtureScript = Join-Path $PSScriptRoot "windows-capture-fixture.ps1"
$WindowsPowerShell = (Get-Command "powershell.exe").Source
$FixtureArguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", ('"{0}"' -f $FixtureScript),
    "-WindowTitle", ('"{0}"' -f $WindowTitle),
    "-ReadyFile", ('"{0}"' -f $ReadyFile),
    "-StopFile", ('"{0}"' -f $StopFile),
    "-WindowMode", $CaptureMode,
    "-MonitorIndex", $MonitorIndex
)
$Fixture = $null

try {
    $StartParameters = @{
        FilePath = $WindowsPowerShell
        ArgumentList = $FixtureArguments
        PassThru = $true
    }
    if ($GameProcessLevel -eq "elevated") {
        $StartParameters.Verb = "RunAs"
    }
    $Fixture = Start-Process @StartParameters

    $Deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not (Test-Path $ReadyFile -PathType Leaf)) {
        if ($Fixture.HasExited) {
            throw "The capture fixture exited before opening its window."
        }
        if ([DateTime]::UtcNow -ge $Deadline) {
            throw "Timed out waiting for the capture fixture."
        }
        Start-Sleep -Milliseconds 250
    }

    $VerifyArguments = @{
        InstallerPath = $InstallerPath
        PreviousInstallerPath = $PreviousInstallerPath
        SmokeTestWindowTitle = $WindowTitle
        SmokeTestModel = $SmokeTestModel
        ExpectedSpeaker = "Marcus"
        VerifyAutoAdvance = $true
        SmokeEvidenceReport = $SmokeEvidenceReport
    }
    if ($GameProcessLevel -eq "elevated") {
        $VerifyArguments.ElevatedSmokeTest = $true
    }
    if ($AllowUnsigned) {
        $VerifyArguments.AllowUnsigned = $true
    }
    & (Join-Path $PSScriptRoot "verify-windows-installer.ps1") @VerifyArguments

    $Signature = Get-AuthenticodeSignature $InstallerPath
    $InstallerVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo(
        $InstallerPath
    ).ProductVersion
    $PreviousInstallerVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo(
        $PreviousInstallerPath
    ).ProductVersion
    if (-not $InstallerVersion -or -not $PreviousInstallerVersion) {
        throw "Both installers must expose a product version."
    }
    $SmokeEvidence = Get-Content $SmokeEvidenceReport -Raw | ConvertFrom-Json
    $Evidence = [ordered]@{
        success = $true
        profile = $ProfileName
        tested_at_utc = [DateTime]::UtcNow.ToString("o")
        operating_system = $OperatingSystem.Caption
        build_number = $BuildNumber
        gpu_vendor = $ExpectedGpuVendor
        gpu_names = $GpuNames
        display_count = $Displays.Count
        monitor_index = $MonitorIndex
        dpi_scale_percent = $DpiScale
        capture_mode = $CaptureMode
        game_process_level = $GameProcessLevel
        installer_signature = $Signature.Status.ToString()
        installer_sha256 = (Get-FileHash $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
        installer_product_version = $InstallerVersion
        installer_signer_subject = if ($Signature.SignerCertificate) {
            $Signature.SignerCertificate.Subject
        } else { $null }
        installer_signer_thumbprint = if ($Signature.SignerCertificate) {
            $Signature.SignerCertificate.Thumbprint.ToLowerInvariant()
        } else { $null }
        previous_installer_sha256 = (Get-FileHash $PreviousInstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
        previous_installer_product_version = $PreviousInstallerVersion
        upgrade_verified = $true
        smoke_test_model = $SmokeTestModel
        smoke_test_process_level = $GameProcessLevel
        auto_advance_dispatched = $SmokeEvidence.auto_advance_dispatched
        auto_advance_acknowledged = $SmokeEvidence.auto_advance_acknowledged
        auto_advance_controller = $SmokeEvidence.auto_advance_controller
    }
    $TemporaryReport = "$EvidenceReport.tmp"
    $Evidence | ConvertTo-Json -Depth 4 | Set-Content $TemporaryReport
    Move-Item $TemporaryReport $EvidenceReport -Force
    Write-Host "Windows release evidence: $EvidenceReport"
}
finally {
    Set-Content -Path $StopFile -Value "stop" -ErrorAction SilentlyContinue
    if ($Fixture -and -not $Fixture.HasExited) {
        $Fixture.WaitForExit(5000) | Out-Null
    }
    Remove-Item $ReadyFile, $StopFile -Force -ErrorAction SilentlyContinue
}
