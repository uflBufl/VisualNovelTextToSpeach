# Opt-in native MOSS timing build

## Windows comparison: complete setup

This is an **opt-in developer measurement**, not normal VNTTS installation.
`git pull` does not download these experimental EXEs. You need two builds,
the existing MOSS GGUF weight pair, and one usable narrator reference.
The comparison itself downloads nothing and does not change app settings.

Use Windows x64 with Git, uv and an existing VNTTS checkout. Run the blocks below
in order in **one PowerShell window already opened in the project folder**.
Close VNTTS and other model applications before the comparison. GPU mode needs
an installed Vulkan-capable driver; the bundled loader does not install drivers.
Windows also needs the [Microsoft Visual C++ x64 runtime](https://aka.ms/vc14/vc_redist.x64.exe)
([Microsoft's download instructions](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)).

### 1. Update the Python environment

```powershell
git pull
if ($LASTEXITCODE -ne 0) { throw 'git pull failed; resolve that before continuing.' }
uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw 'Dependency setup failed; do not run the comparison yet.' }
```

This installs project dependencies, not experimental EXEs or MOSS weights.

### 2. Download and extract both native builds

Open [Native MOSS timing build](https://github.com/uflBufl/VisualNovelTextToSpeach/actions/workflows/native-moss-build.yml)
and choose a successful run containing **both** of these artifacts:

- Baseline: `moss-native-timing-windows-x64`
- Candidate: `moss-native-timing-local-gpu-windows-x64`

This comparison tests GPU audio-frame generation while keeping the waveform
codec on CPU. The older `timing-aux-pool` artifact is a different experiment;
do not substitute it here. If a run lacks the Local GPU artifact or is still
building, wait for a complete successful pair before continuing.

Sign in to GitHub if asked. Save both downloads in your user's `Downloads`
folder with those exact names plus `.zip` (remove browser-added `(1)` suffixes).
If your browser uses another download folder, change `$MossDownloads` below.

**Artifacts expire after 30 days.** If a link is unavailable, open
[Native MOSS timing build](https://github.com/uflBufl/VisualNovelTextToSpeach/actions/workflows/native-moss-build.yml),
choose a successful run, and download **both named artifacts from that same
run** from its Artifacts section. Do not mix runs or use the ordinary upstream
release as one half of this comparison. If no complete pair is available, a
maintainer must run the workflow again; stop here rather than substitute a build.

Each GitHub download is an **outer ZIP containing an inner ZIP and its checksum**.
This block extracts both layers, verifies the inner ZIP checksums, and keeps
each runtime in its own new folder. It never replaces the app's installed runtime.

```powershell
$ErrorActionPreference = 'Stop'
$MossDownloads = Join-Path $env:USERPROFILE 'Downloads'
$MossWork = Join-Path $MossDownloads ("moss-native-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $MossWork) { throw "Folder already exists: $MossWork. Run this block again after a second." }
foreach ($Variant in @('timing', 'timing-local-gpu')) {
    $Name = "moss-native-$Variant-windows-x64"
    $Outer = Join-Path $MossDownloads "$Name.zip"
    if (-not (Test-Path -LiteralPath $Outer -PathType Leaf)) { throw "Download is missing: $Outer" }
    $Downloaded = Join-Path $MossWork "$Variant\download"
    Expand-Archive -LiteralPath $Outer -DestinationPath $Downloaded
    $Inner = Join-Path $Downloaded "$Name.zip"
    $Expected = (Get-Content -LiteralPath "$Inner.sha256" -Raw).Trim()
    if ((Get-FileHash -LiteralPath $Inner -Algorithm SHA256).Hash -ne $Expected) {
        throw "Checksum mismatch: $Inner. Download that artifact again; do not run it."
    }
    Expand-Archive -LiteralPath $Inner -DestinationPath (Join-Path $MossWork "$Variant\runtime")
}
$MossBaseline = Join-Path $MossWork 'timing\runtime\moss-tts-server.exe'
$MossCandidate = Join-Path $MossWork 'timing-local-gpu\runtime\moss-tts-server.exe'
Write-Host "Baseline:  $MossBaseline"
Write-Host "Candidate: $MossCandidate"
```

Keep all extracted files, including DLLs, `VNTTS-BUILD.json` and notices, together.
Do not move just the EXE. Confirm both programs start **without loading weights**:

```powershell
foreach ($Entry in @(
    @($MossBaseline, 'openmoss 0.3.0-vntts-timing1'),
    @($MossCandidate, 'openmoss 0.3.0-vntts-timing1-localgpu')
)) {
    if (-not (Test-Path -LiteralPath $Entry[0] -PathType Leaf)) { throw "EXE is missing: $($Entry[0])" }
    $Version = (& $Entry[0] --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $Version -ne $Entry[1]) {
        throw "Native startup failed or wrong build: $($Entry[0]); exit=$LASTEXITCODE; version=$Version. Check full extraction and the Visual C++ x64 runtime."
    }
    Write-Host $Version
}
```

Expected: the two exact version strings above. A build/startup pass is not an
audio or GPU-performance qualification.

### 3. Locate the weights and narrator reference

**The downloaded artifacts contain no model weights.** You need both
`moss-tts-local-1.5-q8_0.gguf` and
`moss-tts-local-1.5-q8_0.extras.gguf` in the same folder (about 9.1 GB total).
An MLX model directory is not this weight pair.

If MOSS already generates speech in VNTTS on this Windows PC, reuse its files.
The block below first checks the same model location the app uses; it asks for
an existing main GGUF path only if that file is absent. Paste paths without quotes.

```powershell
$MossModel = (uv run --no-sync python -c "from vntts.moss_cpp_installation import configured_paths; from vntts.settings import load_app_settings; print(configured_paths(load_app_settings().tts_model)[1])" | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not read the configured model location.' }
Write-Host "Configured model: $MossModel"
if (-not (Test-Path -LiteralPath $MossModel -PathType Leaf)) {
    $MossModel = Read-Host 'Paste the full path of your existing main .gguf file (not .extras.gguf)'
}
if ([IO.Path]::GetExtension($MossModel) -ne '.gguf' -or $MossModel.EndsWith('.extras.gguf')) {
    throw 'Choose the main .gguf, not an MLX directory or .extras.gguf.'
}
$MossSidecar = [IO.Path]::ChangeExtension($MossModel, '.extras.gguf')
foreach ($File in @($MossModel, $MossSidecar)) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "Required weight file is missing: $File" }
}
```

If you have **neither weight file**, do not continue this benchmark. Run
`uv run vntts-app`, select **MOSS-TTS Local v1.5**, and complete its normal first
speech setup; on Windows x64 the app downloads/verifies the native runtime and
both weights, with progress (about 9.1 GB of weights). Then close the app and
repeat this step. If you use custom runtime/model environment overrides, normal
setup may intentionally not download them; use your existing configured GGUF
pair or remove those overrides in a fresh shell before ordinary setup.

By default, the benchmark uses the **saved Narrator assignment**, which must
resolve to exactly one existing spoken reference WAV: **PCM16 mono, 1–30 seconds**,
with audible speech. A selected character name
alone or a Pocket preset is not sufficient. Save the game narrator selection in
VNTTS first, or add `--reference` with an explicit usable spoken WAV as shown
below. The script checks the reference before starting a server, snapshots it,
and uses that same audio for both builds. It does not silently select another
voice. If preflight rejects a silent/too-short reference, choose a usable spoken
one; do not bypass that check.

### 4. Run the comparison

Keep the same PowerShell window so the paths above are still defined:

```powershell
$MossOutput = Join-Path $MossWork ("comparison-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
uv run --no-sync python -m scripts.moss_native_compare --baseline "$MossBaseline" --candidate "$MossCandidate" --model "$MossModel" --output "$MossOutput"
Write-Host "Exit code: $LASTEXITCODE"
Write-Host "Results: $MossOutput"
Write-Host "Archive: $MossOutput.zip"
```

For an explicit reference, **before running that command**, set
`$MossReference = Read-Host 'Full path to the spoken reference WAV'` and append
`--reference "$MossReference"` to the `uv run` line. Do not run both commands.
Keep the default `--gpu-layers -1` for this experiment. Both builds receive
the CPU-auxiliary setting, but the Local GPU candidate overrides placement
only for its audio-frame model. Its codec and input embeddings remain on CPU.
The candidate requires a GPU; `--gpu-layers 0` is not supported by this build.
No separate CUDA Python environment or MLX installation is needed.

There are **12 generations**: baseline, candidate, candidate, baseline, with
three identical phrases per fresh server, seed 1, production native stable
sampling and synthesis caches bypassed. Startup and the first request are
separate from warm requests. Process-cold does not mean cleared OS/disk caches.
Expect several minutes. Do not start
another model while measuring. Ctrl+C stops the run and preserves partial results
once cleanup finishes; allow it to finish writing rather than killing the shell.

### 5. Results and recovery

Send the **single ZIP printed as `Comparison archive:`**. It contains per-run
reports, timings, available CPU/RSS measurements, input/binary/DLL identities and
generated WAVs. It excludes weights and original reference audio; reports still
contain local paths. Nothing is uploaded automatically.

- Exit 0: all 12 requests completed technically. This is **not** voice approval.
- Exit 1: a failed/limited request, missing file, mismatched build, or another
  technical failure. Read the printed error; partial reports/audio are retained.
- Exit 130: interrupted. Partial results are retained where output was created.
- Missing EXE: recheck step 2 and the printed path; `git pull` cannot install it.
- Missing GGUF/sidecar: complete step 3; do not install MLX to solve this.
- Invalid saved Narrator: supply `--reference` as described in step 4.
- Existing output directory/archive: rerun step 4 for a new timestamp. Do not
  delete previous results merely to retry.
- Checksum/build mismatch: obtain an intact same-run pair; do not bypass checks.
- If ZIP creation fails (for example, full disk), reports already written remain
  in the output directory. Resolve the reported cause before another run.

Failed/limited cases do not count as speed gains. Exact WAV equality is reported,
but the API does not expose codec codes. These measurements do not by themselves
prove acoustic quality, cancellation safety, idle CPU behavior or leak-free
real-model load/unload. Keep the installed default runtime unchanged until those
qualification gates pass.

## Native patch details

This diagnostic patch is based on Apache-2.0 openmoss v0.3.0,
revision `bfb1f465e0a86fb5a52bbf93e67ceba4b7d0b4e1`, with llama.cpp
`050ee92d04c2e1f639025786dea701c70e7d4204`. The baseline and CPU-pool variants add timing and
backbone ownership cleanup, without changing sampling, device placement, thread
counts or audio limits. Modified sections
are marked in the patch. The built server identifies itself as
`0.3.0-vntts-timing1` in `/info` and `--version`.

The **Native MOSS timing build** GitHub Actions workflow builds a separate
Windows Vulkan artifact from these exact revisions. It checks startup without
loading weights; that is not GPU, performance or audio-quality qualification.
It never publishes a release or changes VNTTS's automatic runtime installer.

For a controlled test, extract the entire archive into a separate directory,
keeping its DLLs beside the EXE. Use the existing `scripts/run-moss-windows.ps1`
with `-Server` pointing to that EXE and `-Model` pointing to the existing GGUF.
Leave `-CodecOnGpu` unset on the 8 GB test GPU. Exit that shell afterwards to
discard its runtime overrides. Do not overwrite the managed runtime folder.

## Experimental Local audio-frame model on GPU

The `timing-local-gpu` build enables `OPENMOSS_LOCAL_GPU` (default OFF) and
identifies itself as `0.3.0-vntts-timing1-localgpu`. All builds apply the same
two pinned patches; only the feature options differ. Both patch hashes are
included in the build manifest and must match across a comparison pair.

This candidate keeps CPU-owned input embeddings and the waveform codec, but
uses a separate GPU owner for the Local transformer, its text head and a copy
of the audio embedding tables. It does not move the whole auxiliary sidecar.
The actual split is reported in startup status and `/info`; a different
`same_reported_compute` value in the comparison is intentional, not a failure.

For the pinned Q8 GGUF pair, the extra GPU weight group is about 205 MiB
(145 MiB Local weights plus 60 MiB audio embeddings). These are static tensor
bytes, not a VRAM-fit guarantee: graph buffers, device allocations, context and
other applications also consume memory. Keep `-CodecOnGpu` unset. Do not use
this candidate with a different model architecture or without a GPU.

`moss-local-gpu-check.exe` exercises selection and routing without model weights.
CI compilation and this check do not qualify GPU kernels, speech quality,
real model ownership, VRAM headroom or speed. Compare fresh WAVs and resources
on Windows before adoption; unlike the CPU pool test, different arithmetic
may produce different WAV bytes without proving an audible regression.

First-use reference encoding remains on CPU. The existing
`reference_encoding_s` measurement is included in comparison phase summaries;
do not attribute that one-time work to audio-frame generation. Missing warm
reference timing remains unavailable, not an inferred zero or proven cache hit.

## Experimental auxiliary CPU pool

The workflow also produces a separate `timing-aux-pool` archive, identifying
itself as `0.3.0-vntts-timing1-pool1`. Its only additional change is enabling
`-DOPENMOSS_PERSISTENT_AUX_CPU_POOL=ON` (default `OFF`). The auxiliary CPU
backend retains its four workers across graphs instead of creating and joining
them for every graph. Idle workers sleep (`poll=0`); the disposable baseline
uses GGML's default polling policy. The Aux owner detaches and frees the pool
at quiescent teardown. Backbone and GPU backends are unchanged.

Both archives include `moss-aux-pool-check.exe` and its JSON result. This
no-model check compares repeated synthetic CPU graph output bit-for-bit,
exercises abort recovery and destroys/recreates the real Aux owner. It runs
in CI for both variants. Its timings are **not a speech performance result**.

Keep baseline and experiment in separate directories. Adoption still requires
matching speech output codes with identical inputs/seed, safe request
cancellation and shutdown, and improved cold/warm phase timings without worse
idle CPU or RSS on Windows (CPU-only and CPU-auxiliary modes). Neither artifact
is installed automatically, and no user voice approvals are inferred.

Both variants free the raw libllama context and model during `Model` destruction,
after dependent graph owners and before Aux teardown. Context-creation failure
uses the same owner cleanup instead of a separate manual free. This correction
is shared by both variants so it does not confound the pool comparison. Compile
and synthetic pool checks do not establish leak-free real-model load/unload;
that remains a separate qualification. The packaged `VNTTS-BUILD.json` records
the exact patch hash, since rebuilds may retain the same diagnostic version.

## Measurements

VNTTS reads optional response headers from non-streaming `/tts` and
`/v1/audio/speech` into `native-speech.json` support events:

| Header | Support field | Scope |
| --- | --- | --- |
| `X-MOSS-Backbone-Seconds` | `gen_backbone_s` | Autoregressive libllama decode, including device synchronization |
| `X-MOSS-Frame-Decoder-Seconds` | `gen_frame_decoder_s` | Local depth transformer, sampling, cache work and readback |
| `X-MOSS-Input-Embedding-Seconds` | `gen_input_embedding_s` | Next-row input embeddings and readback |

These are wall-clock phases, not pure kernel times. They exclude prefill and
the waveform codec. Prefill still combines prompt embeddings and main-model
prefill, so adding it to `gen_backbone_s` is not pure main-model time. The
existing `decode_s` measures waveform codec work. The three new phases need
not sum exactly to `gen_s`: loop bookkeeping and any streaming drain are
outside them. Stock runtimes return no new headers, reported as unavailable,
never zero. No speech text, reference paths or per-token traces are added.

The patch synchronizes libllama before stopping its timers; otherwise device
work can be charged to the following phase when the next output is read.
Auxiliary graph calls already synchronize and return host data.

In this pinned runtime, CPU graph planning defaults to four threads for main
model CPU work, and the separate auxiliary backend also defaults to four. No
thread CLI is exposed. Increasing thread counts or moving the full auxiliary
sidecar to GPU requires a separate measured change; neither is done here.

## Third-party notices

The Windows archive includes the x64 Vulkan loader from the pinned SDK runtime
and its original notices in `vulkan-notices`. It does not install drivers or
modify the system runtime; GPU use still needs a compatible installed driver.

The artifact includes openmoss's Apache-2.0 license, llama.cpp's license and
dependency notices. Its server also incorporates cpp-httplib 0.18.7
(Copyright (c) 2025 Yuji Hirose. All rights reserved.) and nlohmann/json 3.12.0
(Copyright (c) 2013-2026 Niels Lohmann), both under the MIT license reproduced
in `LICENSE-cpp-httplib.txt` and `llama-licenses/LICENSE-jsonhpp`.
