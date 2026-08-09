param(
    [Parameter(Mandatory = $true)]
    [string[]]$Files,
    [string]$CertificatePath = $env:VNTTS_SIGNING_CERTIFICATE_PATH,
    [string]$CertificatePassword = $env:VNTTS_SIGNING_CERTIFICATE_PASSWORD,
    [string]$CertificateThumbprint = $env:VNTTS_SIGNING_CERTIFICATE_THUMBPRINT,
    [string]$TimestampUrl = $env:VNTTS_SIGNING_TIMESTAMP_URL
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "Authenticode signing must run on Windows."
}
if (-not $TimestampUrl) {
    $TimestampUrl = "http://timestamp.digicert.com"
}
if (-not $CertificatePath -and -not $CertificateThumbprint) {
    throw "Configure a signing certificate path or certificate thumbprint."
}

$SignTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
if (-not $SignTool) {
    $WindowsKits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path $WindowsKits -PathType Container) {
        $SignTool = Get-ChildItem $WindowsKits -Filter "signtool.exe" -Recurse |
            Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
    }
}
if (-not $SignTool) {
    throw "signtool.exe was not found. Install the Windows SDK signing tools."
}
$SignToolPath = if ($SignTool -is [System.IO.FileInfo]) {
    $SignTool.FullName
}
else {
    $SignTool.Source
}

foreach ($File in $Files) {
    $ResolvedFile = (Resolve-Path $File).Path
    $Arguments = @("sign", "/fd", "SHA256", "/td", "SHA256", "/tr", $TimestampUrl)
    if ($CertificatePath) {
        $Arguments += @("/f", (Resolve-Path $CertificatePath).Path)
        if ($CertificatePassword) {
            $Arguments += @("/p", $CertificatePassword)
        }
    }
    else {
        $Arguments += @("/sha1", $CertificateThumbprint)
    }
    $Arguments += $ResolvedFile

    & $SignToolPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "signtool failed for $ResolvedFile."
    }
    & $SignToolPath verify /pa /all $ResolvedFile
    if ($LASTEXITCODE -ne 0) {
        throw "Authenticode verification failed for $ResolvedFile."
    }
}
