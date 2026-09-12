# Qualify one adaptive Windows MOSS Actions artifact with the production backend.
param(
    [string]$Artifact = (Join-Path $HOME 'Downloads\moss-native-timing-adaptive-windows-x64.zip'),
    [string]$Output = (Join-Path $HOME ("Downloads\moss-adaptive-qualification-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))),
    [ValidatePattern('^[0-9a-fA-F]{7,64}$')][string]$ExpectedBuild = '46454ab'
)
$ErrorActionPreference = 'Stop'
$MaxArchiveBytes = 768MB

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
    $InnerFile = Get-Item -LiteralPath $Inner

    $Runtime = Join-Path $Work 'runtime'
    Expand-Archive -LiteralPath $Inner -DestinationPath $Runtime
    $Server = Join-Path $Runtime 'moss-tts-server.exe'
    $Manifest = Get-Content (Join-Path $Runtime 'VNTTS-BUILD.json') -Raw | ConvertFrom-Json
    $BuildCommit = [string]$Manifest.vntts
    if ($Manifest.variant -ne 'timing-adaptive' -or
        -not $BuildCommit.StartsWith($ExpectedBuild, [StringComparison]::OrdinalIgnoreCase) -or
        $Manifest.ggml_native -ne $false -or
        $Manifest.runtime_controls.persistent_voice_codes -ne $true -or
        $Manifest.runtime_controls.local_gpu -ne $true) {
        throw 'Adaptive build is not portable. Download the latest OpenMOSS Actions artifact.'
    }
    $Capabilities = (& $Server --capabilities-json | Out-String) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or
        $Capabilities.schema -ne 'vntts.openmoss.capabilities' -or
        $Capabilities.version -ne 1 -or
        $Capabilities.local_gpu -ne $true -or
        $Capabilities.aux_cpu_threads -ne $true -or
        $Capabilities.persistent_voice_codes -ne $true) {
        throw 'Adaptive runtime capability contract mismatch.'
    }
    if ($Capabilities.vulkan_available -ne $true) {
        throw 'No Vulkan device is available to the adaptive runtime.'
    }

    $Runs = @(
        @{ Name = 'cpu-2'; Layers = '0'; Workers = '2' },
        @{ Name = 'cpu-4'; Layers = '0'; Workers = '4' },
        @{ Name = 'cpu-6'; Layers = '0'; Workers = '6' },
        @{ Name = 'cpu-8'; Layers = '0'; Workers = '8' },
        @{ Name = 'gpu-8'; Layers = '-1'; Workers = '8' }
    )
    [Environment]::SetEnvironmentVariable('VNTTS_MOSS_QUALIFY_ADAPTIVE', '1', 'Process')
    [Environment]::SetEnvironmentVariable('VNTTS_MOSS_AUX_CPU', '1', 'Process')
    $Reports = @{}
    $Index = 0
    foreach ($Run in $Runs) {
        $Index++
        [Environment]::SetEnvironmentVariable('VNTTS_MOSS_GPU_LAYERS', $Run.Layers, 'Process')
        [Environment]::SetEnvironmentVariable('VNTTS_MOSS_AUX_CPU_THREADS', $Run.Workers, 'Process')
        Write-Host ("[{0}/{1}] {2}" -f $Index, $Runs.Count, $Run.Name)
        $Extra = @('--timing-sequence')
        if ($Run.Name -eq 'gpu-8') {
            $Extra += @('--cancel-restart', '--require-changing-voice')
        }
        & uv run --frozen python scripts/moss_native_pause_probe.py `
            --executable $Server --output (Join-Path $Output $Run.Name) @Extra
        if ($LASTEXITCODE -ne 0) { throw "$($Run.Name) probe failed." }
        $Reports[$Run.Name] = Get-Content (Join-Path $Output "$($Run.Name)\report.json") -Raw | ConvertFrom-Json
        $Attempts = @($Reports[$Run.Name].attempts)
        $Invalid = @($Attempts | Where-Object {
            $_.completion -ne 'complete' -or
            $_.result.cache_source -ne 'fresh-generation' -or
            $_.raw_response.http_status -ne 200 -or
            [string]::IsNullOrWhiteSpace([string]$_.raw_response.sha256) -or
            $_.raw_quality_error -or
            $_.native.operation -ne 'fresh-generation' -or
            $null -eq $_.native.request_s -or
            $null -eq $_.native.prefill_s -or
            $null -eq $_.native.gen_s -or
            $null -eq $_.native.decode_s -or
            $null -eq $_.output_wav_validation_s -or
            $null -eq $_.raw_wav_validation_s
        })
        if ($Reports[$Run.Name].all_requests_complete -ne $true -or
            $Attempts.Count -ne $Reports[$Run.Name].expected_attempt_count -or
            $Invalid.Count -ne 0) {
            throw "$($Run.Name) contains incomplete or invalid render evidence."
        }
        if ($Reports[$Run.Name].server_shutdown.confirmed_exited -ne $true) {
            throw "$($Run.Name) did not confirm native server shutdown."
        }
        Remove-Item -LiteralPath (Join-Path $Output "$($Run.Name).zip") -Force
    }
    $CpuRuns = @($Runs | Where-Object { $_.Name -like 'cpu-*' })
    $ExpectedHashes = @($Reports[$CpuRuns[0].Name].attempts | ForEach-Object { $_.raw_response.sha256 })
    foreach ($ExpectedRun in $CpuRuns) {
        $Hashes = @($Reports[$ExpectedRun.Name].attempts | ForEach-Object { $_.raw_response.sha256 })
        if ($ExpectedHashes.Count -eq 0 -or $ExpectedHashes.Count -ne $Hashes.Count -or
            (Compare-Object $ExpectedHashes $Hashes -SyncWindow 0)) {
            throw "CPU WAV hashes differ for $($ExpectedRun.Name)."
        }
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
    $CancelRestart = $Reports['gpu-8'].cancel_restart
    if ($CancelRestart.cancelled_completion -ne 'cancelled' -or
        $CancelRestart.cancelled_outcome -ne 'cancelled' -or
        $CancelRestart.cancelled_server.confirmed_exited -ne $true -or
        $CancelRestart.restart_server_pid -eq $CancelRestart.cancelled_server.pid) {
        throw 'gpu-8 cancellation/restart contract failed.'
    }
    $Restart = @($Reports['gpu-8'].attempts | Where-Object { $_.id -match '-restart$' })[-1]
    if ($Restart.native.reference -ne 'cached-codes' -or
        $Restart.native.reference_encoding_s -ne $null) {
        throw 'gpu-8 restart did not restore persistent reference codes.'
    }
    $TimingPhases = @{}
    foreach ($Attempt in $Reports['gpu-8'].attempts) {
        if ($Attempt.phase -in @('process-cold', 'same-voice-warm', 'changed-voice-cold', 'changed-voice-warm')) {
            $TimingPhases[$Attempt.phase] = $Attempt
        }
    }
    if ($TimingPhases.Count -ne 4 -or
        $TimingPhases['process-cold'].native.reference -ne 'encoded' -or
        $null -eq $TimingPhases['process-cold'].native.reference_encoding_s -or
        $TimingPhases['same-voice-warm'].native.reference -ne 'cached-codes' -or
        $TimingPhases['same-voice-warm'].native.reference_encoding_s -ne $null -or
        $TimingPhases['changed-voice-cold'].native.reference -ne 'encoded' -or
        $null -eq $TimingPhases['changed-voice-cold'].native.reference_encoding_s -or
        $TimingPhases['changed-voice-warm'].native.reference -ne 'cached-codes' -or
        $TimingPhases['changed-voice-warm'].native.reference_encoding_s -ne $null) {
        throw 'gpu-8 cold/warm and changed-voice timing contract failed.'
    }

    $RunSummary = @{}
    foreach ($Run in $Runs) {
        $RunSummary[$Run.Name] = @{
            runtime = $Reports[$Run.Name].runtime
            compute = $Reports[$Run.Name].compute
            attempts = @($Reports[$Run.Name].attempts | ForEach-Object {
                @{
                    id = $_.id
                    phase = $_.phase
                    elapsed_seconds = $_.elapsed_seconds
                    output_wav_validation_s = $_.output_wav_validation_s
                    raw_wav_validation_s = $_.raw_wav_validation_s
                    native = $_.native
                    raw_wav_sha256 = $_.raw_response.sha256
                }
            })
        }
    }
    $RunSummary['gpu-8']['cancel_restart'] = $CancelRestart

    $Summary = [ordered]@{
        schema = 'vntts.moss-adaptive-qualification'
        schema_version = 1
        qualified = $true
        contains_generated_voice_audio = $true
        artifact = @{
            name = [IO.Path]::GetFileName($Artifact)
            sha256 = (Get-FileHash -LiteralPath $Artifact -Algorithm SHA256).Hash
            bytes = (Get-Item -LiteralPath $Artifact).Length
        }
        runtime_archive = @{
            name = $InnerFile.Name
            sha256 = $Actual
            bytes = $InnerFile.Length
        }
        build = $Manifest
        capabilities = $Capabilities
        cpu_wav_identity = $true
        runs = $RunSummary
        all_servers_confirmed_stopped = $true
    }
    $Summary | ConvertTo-Json -Depth 20 | Set-Content (Join-Path $Output 'qualification.json') -Encoding utf8
    $ArchiveInputBytes = (Get-ChildItem -LiteralPath $Output -Recurse -File | Measure-Object -Property Length -Sum).Sum
    if ($ArchiveInputBytes -gt $MaxArchiveBytes) {
        throw "Qualification artifacts exceed the $MaxArchiveBytes-byte archive limit."
    }
    $PublicationStarted = Get-Date
    $Archive = "$Output.zip"
    Compress-Archive -Path (Join-Path $Output '*') -DestinationPath $Archive
    $ArchiveFile = Get-Item -LiteralPath $Archive
    if ($ArchiveFile.Length -gt $MaxArchiveBytes) {
        throw "Qualification archive exceeds the $MaxArchiveBytes-byte limit."
    }
    $ArchiveHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash
    $PublicationSeconds = ((Get-Date) - $PublicationStarted).TotalSeconds
    $ArchiveHash | Set-Content "${Archive}.sha256" -Encoding ascii
    [ordered]@{
        schema = 'vntts.moss-adaptive-qualification-receipt'
        schema_version = 1
        qualified = $true
        publication_seconds = [Math]::Round($PublicationSeconds, 3)
        archive = @{ name = $ArchiveFile.Name; sha256 = $ArchiveHash; bytes = $ArchiveFile.Length }
    } | ConvertTo-Json -Depth 4 | Set-Content "${Archive}.json" -Encoding utf8
    Write-Host "Qualification complete: $Archive"
} catch {
    if ($Archive) {
        foreach ($Partial in @($Archive, "${Archive}.sha256", "${Archive}.json")) {
            if (Test-Path -LiteralPath $Partial) {
                Remove-Item -LiteralPath $Partial -Force
            }
        }
    }
    if (Test-Path -LiteralPath $Output -PathType Container) {
        [ordered]@{
            schema = 'vntts.moss-adaptive-qualification'
            schema_version = 1
            qualified = $false
            contains_generated_voice_audio = [bool](Get-ChildItem -LiteralPath $Output -Recurse -Filter '*.wav' -File -ErrorAction SilentlyContinue)
            error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
        } | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $Output 'qualification.json') -Encoding utf8
        Write-Host "Qualification failed; partial evidence retained in: $Output"
    }
    throw
} finally {
    foreach ($Name in $Names) {
        [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], 'Process')
    }
    if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
}
