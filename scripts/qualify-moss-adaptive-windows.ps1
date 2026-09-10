# Qualify one adaptive Windows MOSS Actions artifact with the production backend.
param(
    [string]$Artifact = (Join-Path $HOME 'Downloads\moss-native-timing-adaptive-windows-x64.zip'),
    [string]$Output = (Join-Path $HOME ("Downloads\moss-adaptive-qualification-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss')))
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath 'pyproject.toml' -PathType Leaf)) {
    throw 'Run this command from the VNTTS project folder.'
}
if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
    throw "Adaptive Actions artifact not found: $Artifact"
}
if (Test-Path -LiteralPath $Output) {
    throw "Output already exists: $Output"
}

$Artifact = (Resolve-Path -LiteralPath $Artifact).Path
$Output = [IO.Path]::GetFullPath($Output)
$Work = Join-Path ([IO.Path]::GetTempPath()) ("vntts-moss-adaptive-{0}" -f [guid]::NewGuid())
$Names = @(
    'VNTTS_MOSS_QUALIFY_ADAPTIVE',
    'VNTTS_MOSS_GPU_LAYERS',
    'VNTTS_MOSS_AUX_CPU',
    'VNTTS_MOSS_AUX_CPU_THREADS'
)
$Previous = @{}
foreach ($Name in $Names) {
    $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
}

try {
    New-Item -ItemType Directory -Path $Work, $Output | Out-Null
    $Download = Join-Path $Work 'download'
    Expand-Archive -LiteralPath $Artifact -DestinationPath $Download
    $Inner = Join-Path $Download 'moss-native-timing-adaptive-windows-x64.zip'
    $Checksum = "$Inner.sha256"
    if (-not (Test-Path -LiteralPath $Inner -PathType Leaf) -or
        -not (Test-Path -LiteralPath $Checksum -PathType Leaf)) {
        throw 'The ZIP is not the timing-adaptive Actions artifact.'
    }
    $Expected = (Get-Content -LiteralPath $Checksum -Raw).Trim()
    $Actual = (Get-FileHash -LiteralPath $Inner -Algorithm SHA256).Hash
    if ($Actual -ne $Expected) { throw 'Adaptive artifact checksum mismatch.' }

    $Runtime = Join-Path $Work 'runtime'
    Expand-Archive -LiteralPath $Inner -DestinationPath $Runtime
    $Server = Join-Path $Runtime 'moss-tts-server.exe'
    $Manifest = Get-Content (Join-Path $Runtime 'VNTTS-BUILD.json') -Raw | ConvertFrom-Json
    if ($Manifest.variant -ne 'timing-adaptive' -or
        $Manifest.runtime_controls.local_gpu -ne $true) {
        throw 'Adaptive build manifest contract mismatch.'
    }
    $Capabilities = (& $Server --capabilities-json | Out-String) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or
        $Capabilities.schema -ne 'vntts.openmoss.capabilities' -or
        $Capabilities.version -ne 1 -or
        $Capabilities.local_gpu -ne $true -or
        $Capabilities.aux_cpu_threads -ne $true) {
        throw 'Adaptive runtime capability contract mismatch.'
    }
    if ($Capabilities.vulkan_available -ne $true) {
        throw 'No Vulkan device is available to the adaptive runtime.'
    }

    $Runs = @(
        @{ Name = 'cpu-4'; Layers = '0'; Workers = '4' },
        @{ Name = 'cpu-8'; Layers = '0'; Workers = '8' },
        @{ Name = 'gpu-8'; Layers = '-1'; Workers = '8' }
    )
    [Environment]::SetEnvironmentVariable('VNTTS_MOSS_QUALIFY_ADAPTIVE', '1', 'Process')
    [Environment]::SetEnvironmentVariable('VNTTS_MOSS_AUX_CPU', '1', 'Process')
    $Index = 0
    foreach ($Run in $Runs) {
        $Index++
        [Environment]::SetEnvironmentVariable('VNTTS_MOSS_GPU_LAYERS', $Run.Layers, 'Process')
        [Environment]::SetEnvironmentVariable('VNTTS_MOSS_AUX_CPU_THREADS', $Run.Workers, 'Process')
        Write-Host ("[{0}/3] {1}" -f $Index, $Run.Name)
        & uv run --frozen python scripts/moss_native_pause_probe.py `
            --executable $Server --output (Join-Path $Output $Run.Name)
        if ($LASTEXITCODE -ne 0) { throw "$($Run.Name) probe failed." }
        Remove-Item -LiteralPath (Join-Path $Output "$($Run.Name).zip") -Force
    }

    $Reports = @{}
    foreach ($Run in $Runs) {
        $Reports[$Run.Name] = Get-Content (Join-Path $Output "$($Run.Name)\report.json") -Raw | ConvertFrom-Json
        if ($Reports[$Run.Name].server_shutdown.confirmed_exited -ne $true) {
            throw "$($Run.Name) did not confirm native server shutdown."
        }
    }
    $Cpu4 = @($Reports['cpu-4'].attempts | ForEach-Object { $_.raw_response.sha256 })
    $Cpu8 = @($Reports['cpu-8'].attempts | ForEach-Object { $_.raw_response.sha256 })
    if ($Cpu4.Count -eq 0 -or $Cpu4.Count -ne $Cpu8.Count -or
        (Compare-Object $Cpu4 $Cpu8 -SyncWindow 0)) {
        throw 'CPU 4/8-worker WAV hashes differ.'
    }
    foreach ($ExpectedRun in @(
        @{ Name = 'cpu-4'; Workers = 4 },
        @{ Name = 'cpu-8'; Workers = 8 }
    )) {
        $Placement = $Reports[$ExpectedRun.Name].runtime.placement
        if ($Placement.gpu_layers -ne 0 -or
            $Placement.aux_cpu_threads -ne $ExpectedRun.Workers -or
            $Placement.local -notmatch '^CPU') {
            throw "$($ExpectedRun.Name) reported unexpected device placement."
        }
    }
    $GpuPlacement = $Reports['gpu-8'].runtime.placement
    if ($GpuPlacement.gpu_layers -le 0 -or
        $GpuPlacement.aux_cpu_threads -ne 8 -or
        $GpuPlacement.local -notmatch 'Vulkan' -or
        [string]::IsNullOrWhiteSpace($GpuPlacement.device)) {
        throw 'gpu-8 reported unexpected device placement.'
    }

    $Summary = [ordered]@{
        schema = 'vntts.moss-adaptive-qualification'
        schema_version = 1
        artifact = @{ path = $Artifact; sha256 = (Get-FileHash $Artifact -Algorithm SHA256).Hash }
        build = $Manifest
        capabilities = $Capabilities
        cpu_wav_identity = $true
        runs = @{
            'cpu-4' = @{ runtime = $Reports['cpu-4'].runtime; compute = $Reports['cpu-4'].compute }
            'cpu-8' = @{ runtime = $Reports['cpu-8'].runtime; compute = $Reports['cpu-8'].compute }
            'gpu-8' = @{ runtime = $Reports['gpu-8'].runtime; compute = $Reports['gpu-8'].compute }
        }
        all_servers_confirmed_stopped = $true
    }
    $Summary | ConvertTo-Json -Depth 20 | Set-Content (Join-Path $Output 'qualification.json') -Encoding utf8
    Compress-Archive -Path (Join-Path $Output '*') -DestinationPath "$Output.zip"
    Write-Host "Qualification complete: $Output.zip"
} finally {
    foreach ($Name in $Names) {
        [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], 'Process')
    }
    if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
}
