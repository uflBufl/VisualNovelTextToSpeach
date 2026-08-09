param(
    [Parameter(Mandatory = $true)]
    [string]$WindowTitle,
    [Parameter(Mandatory = $true)]
    [string]$ReadyFile,
    [Parameter(Mandatory = $true)]
    [string]$StopFile,
    [ValidateSet("windowed", "borderless")]
    [string]$WindowMode = "windowed",
    [int]$MonitorIndex = 0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "The capture fixture must run on Windows."
}

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class VnttsFixtureDpi {
    [DllImport("user32.dll")]
    public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}
"@
[VnttsFixtureDpi]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null

$Screens = [System.Windows.Forms.Screen]::AllScreens
if ($MonitorIndex -lt 0 -or $MonitorIndex -ge $Screens.Count) {
    throw "Monitor index $MonitorIndex is not available."
}
$Screen = $Screens[$MonitorIndex]
$Form = New-Object System.Windows.Forms.Form
$Form.Text = $WindowTitle
$Form.StartPosition = "Manual"
$Form.ClientSize = New-Object System.Drawing.Size(1000, 700)
$Form.Location = New-Object System.Drawing.Point(
    ($Screen.WorkingArea.Left + 40),
    ($Screen.WorkingArea.Top + 40)
)
$Form.BackColor = [System.Drawing.Color]::FromArgb(20, 25, 35)
$Form.TopMost = $true
if ($WindowMode -eq "borderless") {
    $Form.FormBorderStyle = "None"
}

$Scene = New-Object System.Windows.Forms.Label
$Scene.Location = New-Object System.Drawing.Point(0, 0)
$Scene.Size = New-Object System.Drawing.Size(1000, 476)
$Scene.Text = "Visual novel capture compatibility fixture"
$Scene.ForeColor = [System.Drawing.Color]::LightGray
$Scene.Font = New-Object System.Drawing.Font("Segoe UI", 20)
$Scene.TextAlign = "MiddleCenter"
$Form.Controls.Add($Scene)

$Dialog = New-Object System.Windows.Forms.Label
$Dialog.Location = New-Object System.Drawing.Point(0, 476)
$Dialog.Size = New-Object System.Drawing.Size(1000, 224)
$Dialog.Padding = New-Object System.Windows.Forms.Padding(32, 18, 32, 18)
$Dialog.BackColor = [System.Drawing.Color]::FromArgb(245, 245, 245)
$Dialog.ForeColor = [System.Drawing.Color]::Black
$Dialog.Font = New-Object System.Drawing.Font("Segoe UI", 24)
$Dialog.Text = "Marcus`r`n`r`nCompatibility capture and speech are working."
$Form.Controls.Add($Dialog)

$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = 250
$Timer.Add_Tick({
    if (Test-Path $StopFile -PathType Leaf) {
        $Form.Close()
    }
})
$Form.Add_Shown({
    Set-Content -Path $ReadyFile -Value $Form.Handle.ToInt64()
    $Form.Activate()
    $Timer.Start()
})
$Form.Add_FormClosed({ $Timer.Stop() })
[System.Windows.Forms.Application]::Run($Form)
