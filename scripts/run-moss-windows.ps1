# Run MOSS Local v1.5 with an existing openmoss Windows release and GGUF pair.
# Binaries: https://github.com/pwilkin/openmoss/releases
# Weights: https://huggingface.co/ilintar/moss-tts-local-gguf/tree/main
# Keep the release DLLs beside moss-tts-server.exe. The matching sidecar must
# be named <model-name>.extras.gguf beside <model-name>.gguf.
param(
    [Parameter(Mandatory=$true)][string]$Server,
    [Parameter(Mandatory=$true)][string]$Model,
    [ValidateRange(-1, 1000)][int]$GpuLayers = -1,
    [switch]$CodecOnGpu,
    [string]$NarratorReference,
    [string]$Application
)
$ErrorActionPreference = 'Stop'
foreach ($RequiredFile in @($Server, $Model, [IO.Path]::ChangeExtension($Model, 'extras.gguf'))) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Missing MOSS file: $RequiredFile"
    }
}
$env:VNTTS_MOSS_CPP_EXECUTABLE = (Resolve-Path -LiteralPath $Server).Path
$env:VNTTS_MOSS_GGUF = (Resolve-Path -LiteralPath $Model).Path
$env:VNTTS_MOSS_GPU_LAYERS = "$GpuLayers"
$env:VNTTS_MOSS_AUX_CPU = if ($CodecOnGpu) { '0' } else { '1' }
if ($Application) {
    & $Application
} elseif ($NarratorReference) {
    # A fresh, device-independent render using the production backend factory.
    uv run --no-sync vntts-benchmark-tts --backend moss-tts --model $env:VNTTS_MOSS_GGUF --character Narrator --narrator-reference $NarratorReference
} else {
    uv run --no-sync vntts-app
}
if ($LASTEXITCODE -ne 0) { throw "VNTTS exited with code $LASTEXITCODE" }
