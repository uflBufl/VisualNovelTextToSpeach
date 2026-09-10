# Opt-in native MOSS timing build

## Windows: compare four versus eight CPU codec workers

This is an **opt-in developer comparison**, not normal app setup. Use it after
the Local GPU comparison, whose three candidate clips are already
accepted. This changes **only CPU auxiliary worker count**; both builds keep
Local audio-frame generation on GPU, codec/reference encoding on CPU and the
persistent CPU pool OFF. More workers are an experiment, not a promised speedup.
Production settings are not changed.

### Before starting

Use Windows x64, an up-to-date VNTTS checkout, uv, a Vulkan-capable GPU driver
and the [Microsoft Visual C++ x64 runtime](https://aka.ms/vc14/vc_redist.x64.exe).
Run commands from the project folder. Close VNTTS and other model applications.

You need the existing MOSS Local 1.5 GGUF pair (about 9.1 GB) and a saved game
Narrator reference. If ordinary MOSS speech already works, use those same files.
If not, complete MOSS setup and save a game narrator in `uv run vntts-app`,
then **close the app**. An MLX directory or Pocket preset is not sufficient.
This comparison does not download weights or silently substitute a voice.

### Download

Open [Native MOSS timing build](https://github.com/uflBufl/VisualNovelTextToSpeach/actions/workflows/native-moss-build.yml).
From **one successful run**, save these two artifacts in Downloads:

- `moss-native-timing-local-gpu-windows-x64.zip` (four workers, baseline)
- `moss-native-timing-local-gpu-aux8-windows-x64.zip` (eight workers, candidate)

Keep the exact names; remove browser-added `(1)` suffixes. **Do not extract them**
and do not launch any EXE manually. Neither `timing` nor `timing-aux-pool` is
part of this comparison. Use a fresh matching pair: both manifests must identify
the auxiliary-thread patch. Artifacts expire after 30 days; if missing,
choose another successful run containing both variants, never mix runs.
The tool compares source and patch identities, not GitHub run IDs; select the
pair from one run yourself even when two runs use the same source commit.

### Run

After updating the checkout with `git pull`, run this **single PowerShell command**:

```powershell
uv run --frozen python -m scripts.moss_native_compare --experiment codec-threads
```

Keep `--experiment codec-threads` in the command. Omitting it runs the older
Local GPU OFF/ON comparison, with four workers in both builds, not this test.
Use this checkout's guide; README copies bundled in older ZIPs are snapshots
and may still describe that earlier experiment.

It installs any missing locked Python dependencies, then:

1. Checks and extracts both ZIP layers into a new `Downloads/moss-native-...`
   folder, keeping the DLLs and manifests together. Verifies checksums, matching
   build provenance and exact EXE versions using short, timed `--version` calls.
2. Reads the saved GGUF location and Narrator reference, checks audible PCM16
   mono speech (1–30 seconds), and snapshots the reference for the comparison.
3. Runs **12 generations**, using one owned server at a time:
   baseline, candidate, candidate, baseline; three phrases per server.
   Each server is shut down before the next one starts. Reports record exit
   confirmation for every observed owned process, including replacement servers.
4. Prints `Comparison archive:` with the ZIP to send back. Nothing is uploaded.

No separate server-start, warm-up or application-launch command is needed.
The first request includes reference encoding; later requests are measured
separately. Expect several minutes. Keep other model applications closed.

**To cancel:** press Ctrl+C once and wait for cleanup and partial-report writing.
Do not close/force-kill the terminal while cleanup is running. Normal completion,
handled errors and Ctrl+C run owned-server cleanup; forcibly terminating the
Python process or closing the terminal is not a guaranteed cleanup path.

### Optional paths and retrying

Only if Downloads is elsewhere:

```powershell
uv run --frozen python -m scripts.moss_native_compare --experiment codec-threads --downloads "D:\Downloads"
```

Only if the saved model or narrator is not the one you want:

```powershell
uv run --frozen python -m scripts.moss_native_compare --experiment codec-threads --model "D:\Models\moss-tts-local-1.5-q8_0.gguf" --reference "D:\Voices\centurion.wav"
```

These are **alternatives**, not additional steps. A retry uses the same command
and creates a fresh folder; there are no shell variables to restore. Existing
outputs are never overwritten. A missing/invalid input is reported before
generation; fix the named file or saved voice instead of bypassing its check.
The sidecar must be next to the main model, named `<model>.extras.gguf`.

Keep the default `--gpu-layers -1`. This candidate requires a GPU; do not use
`--gpu-layers 0` or enable the full auxiliary codec on GPU. No CUDA Python or
MLX environment is needed for this C++/Vulkan test.

### Results

Send the single ZIP printed as **Comparison archive**. It contains timings,
available CPU/RSS/GPU measurements, binary identities, reports and generated
speech. It excludes weights and the original reference, but reports contain
local paths. The unarchived reference snapshot stays in your work folder.

We compare codec/reference time, total time, memory and exact WAV identity.
Byte-identical output keeps its existing listening approval; changed audio needs
listening. Missing process-exit confirmation is not inferred from an empty error
list. Keep the installed production runtime unchanged until qualification passes.

- Exit 0: all 12 requests completed technically, **not** acoustic approval.
- Exit 1: failure; read the printed reason. Partial results are retained.
- Exit 130: interrupted; existing results are retained.
- ZIP creation failure: reports remain in the output folder; resolve the cause
  (for example, full disk) before retrying.
- Startup/DLL error: verify the Visual C++ runtime and download an intact pair.
- Suspected leftover from an older run: close the app and check Task Manager
  before retrying. This command does not kill unrelated existing servers.

After sending results, the printed `moss-native-...` work folder may be deleted
when no comparison is running; it contains extracted experimental binaries,
the reference snapshot and results, not the installed app or model weights.

For developers comparing other trusted, already extracted builds, the existing
advanced interface remains: `--baseline PATH --candidate PATH --model PATH
--output NEW_FOLDER` (and optional `--reference PATH`). Explicit EXE mode skips
archive and version checks; it compares manifest source identities only when
both manifests exist. It is not the validated download setup described above.
Ordinary users need none of these EXE paths.
Failed/limited generations never count as speed gains; real Windows audio,
memory and shutdown qualification is still required before adopting the variant.

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

For the controlled test, use the comparison command above. The separate
`scripts/run-moss-windows.ps1` launcher opens an interactive app; it is **not**
a prerequisite for the comparison. Do not overwrite the managed runtime folder.

## Experimental Local audio-frame model on GPU

The `timing-local-gpu` build enables `OPENMOSS_LOCAL_GPU` (default OFF) and
identifies itself as `0.3.0-vntts-timing1-localgpu`. All builds apply the same
three pinned patches (timings, Local GPU and auxiliary threads); only the feature
options differ. All three patch hashes are
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
The separate GPU path checks graph operation support before execution and
returns the backend and unsupported operation name instead of attempting it.

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
thread CLI is exposed. The `timing-local-gpu-aux8` build sets
`OPENMOSS_AUX_CPU_THREADS=8` and identifies itself as
`0.3.0-vntts-timing1-localgpu-aux8`; only auxiliary workers change, not main-model
CPU threads. Moving the full auxiliary sidecar to GPU remains a separate,
unqualified change.

## Third-party notices

The Windows archive includes the x64 Vulkan loader from the pinned SDK runtime
and its original notices in `vulkan-notices`. It does not install drivers or
modify the system runtime; GPU use still needs a compatible installed driver.

The artifact includes openmoss's Apache-2.0 license, llama.cpp's license and
dependency notices. Its server also incorporates cpp-httplib 0.18.7
(Copyright (c) 2025 Yuji Hirose. All rights reserved.) and nlohmann/json 3.12.0
(Copyright (c) 2013-2026 Niels Lohmann), both under the MIT license reproduced
in `LICENSE-cpp-httplib.txt` and `llama-licenses/LICENSE-jsonhpp`.
