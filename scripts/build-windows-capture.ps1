$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") {
    throw "Build the Windows capture helper on Windows with the Rust MSVC toolchain."
}
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CrateRoot = Join-Path $ProjectRoot "native\capture-worker\windows"
$Manifest = Join-Path $CrateRoot "Cargo.toml"
# Keep source-install discovery deterministic, including when CARGO_TARGET_DIR is set.
$TargetDirectory = Join-Path $CrateRoot "target"
& cargo test --locked --manifest-path $Manifest --target-dir $TargetDirectory
if ($LASTEXITCODE -ne 0) { throw "Windows capture helper tests failed." }
& cargo build --locked --release --target x86_64-pc-windows-msvc `
    --manifest-path $Manifest --target-dir $TargetDirectory
if ($LASTEXITCODE -ne 0) { throw "Windows capture helper build failed." }
$BuiltBinary = Join-Path $TargetDirectory "x86_64-pc-windows-msvc\release\vntts-capture-worker.exe"
$ReleaseDirectory = Join-Path $TargetDirectory "release"
New-Item -ItemType Directory -Force -Path $ReleaseDirectory | Out-Null
$Binary = Join-Path $ReleaseDirectory "vntts-capture-worker.exe"
Copy-Item -LiteralPath $BuiltBinary -Destination $Binary -Force
if (-not (Test-Path -LiteralPath $Binary -PathType Leaf)) {
    throw "Expected x64 Windows capture helper was not produced: $Binary"
}
Write-Output $Binary
